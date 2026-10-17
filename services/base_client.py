import os
import time
import httpx
import logging
import asyncio
from typing import Dict, Optional, List
from database import supabase

logger = logging.getLogger("uvicorn.error")

BASE_API_URL = "https://workflow.base.vn/extapi/v1"
BASE_API_TIMEOUT = 30.0       # tăng từ 15s lên 30s
BASE_API_RETRIES = 3          # thử lại khi lỗi mạng
BASE_API_RETRY_DELAY = 1.0    # giây nghỉ giữa các lần retry


class BaseClient:
    def __init__(self):
        self._cache: Optional[Dict[str, str]] = None
        self._last_updated: float = 0
        self._ttl: float = 60.0

    async def _get_settings(self) -> Dict[str, str]:
        now = time.time()
        if self._cache and (now - self._last_updated) < self._ttl:
            return self._cache
        try:
            res = await asyncio.to_thread(
                lambda: supabase.table("system_settings").select("key, value").execute()
            )
            if res.data:
                self._cache = {
                    str(item["key"]).strip(): str(item["value"]).strip()
                    for item in res.data
                }
                self._last_updated = now
                return self._cache
        except Exception as e:
            logger.error(f"Lỗi tải system_settings: {e}")

        # Fallback từ env — đủ cả 3 quy trình (trước đây thiếu storage/delivery)
        return {
            "base_repair_token": os.getenv("BASE_REPAIR_TOKEN", ""),
            "base_storage_token": os.getenv("BASE_STORAGE_TOKEN", ""),
            "base_delivery_token": os.getenv("BASE_DELIVERY_TOKEN", ""),
            "base_warranty_token": os.getenv("BASE_WARRANTY_TOKEN", ""),
            "base_recept_token": os.getenv("BASE_RECEPT_TOKEN", ""),
            "base_table_token": os.getenv("BASE_TABLE_TOKEN", ""),
            "workflow_repair_id": os.getenv("WORKFLOW_REPAIR_ID", "20025"),
            "workflow_storage_id": os.getenv("WORKFLOW_STORAGE_ID", "20036"),
            "workflow_delivery_id": os.getenv("WORKFLOW_DELIVERY_ID", "20037"),
        }

    async def get_token_and_wf(self, app_name: str = "repair") -> tuple[str, str]:
        settings = await self._get_settings()
        app = app_name.lower().strip()

        token = settings.get(f"base_{app}_token", "")
        # ⚠️ KHÔNG dùng base_shared_secret làm access_token gọi Base API.
        # base_shared_secret chỉ dùng để xác thực webhook gửi đến server.
        if not token:
            token = os.getenv(f"BASE_{app.upper()}_TOKEN", "")

        wf_id = (
            settings.get(f"workflow_{app}_id")
            or settings.get(f"workflow_{app}", "")
            or os.getenv(f"WORKFLOW_{app.upper()}_ID", "")
        )
        return token, str(wf_id).strip()

    async def fetch_jobs(self, app_name: str = "repair") -> Optional[List[dict]]:
        """Lấy toàn bộ Jobs (phân trang + retry). Trả None nếu lỗi kết nối/cấu hình/body báo lỗi."""
        token, wf_id = await self.get_token_and_wf(app_name)
        if not token or not wf_id:
            logger.error(
                f"Thiếu Token/Workflow ID cho app={app_name} "
                f"(token={'có' if token else 'không'}, wf='{wf_id}')"
            )
            return None

        all_jobs: List[dict] = []
        seen_ids = set()
        MAX_PAGES = 100

        async with httpx.AsyncClient(timeout=BASE_API_TIMEOUT) as client:
            try:
                for page in range(MAX_PAGES):
                    # --- Retry cho từng trang ---
                    data = None
                    last_err = None
                    for attempt in range(BASE_API_RETRIES):
                        try:
                            res = await client.post(
                                f"{BASE_API_URL}/jobs/get",
                                data={"access_token": token, "workflow_id": wf_id, "page": page},
                            )
                            if res.status_code != 200:
                                logger.error(f"Base API HTTP {res.status_code} page={page}: {res.text[:200]}")
                                if res.status_code in (401, 403, 404):
                                    return None  # lỗi cấu hình, không cần retry
                                last_err = f"HTTP {res.status_code}"
                                await asyncio.sleep(BASE_API_RETRY_DELAY)
                                continue
                            data = res.json()
                            break
                        except Exception as e:
                            last_err = e
                            await asyncio.sleep(BASE_API_RETRY_DELAY)

                    if data is None:
                        logger.error(f"Base API thất bại sau {BASE_API_RETRIES} lần (page={page}): {last_err}")
                        return None

                    logger.info(
                        f"[FETCH-JOBS] app={app_name} wf={wf_id} page={page} "
                        f"code={data.get('code')} n_jobs={len(data.get('jobs') or [])}"
                    )

                    # Base báo lỗi bằng body, không phải HTTP status
                    if str(data.get("code", 1)) not in ("1", "True", "true"):
                        logger.error(f"Base API trả lỗi trong body: {data}")
                        return None

                    jobs = data.get("jobs") or []

                    # Trang rỗng = hết dữ liệu
                    if not jobs:
                        break

                    new_jobs = [j for j in jobs if str(j.get("id")) not in seen_ids]
                    # Toàn bộ job trong trang đã thấy = đã qua trang cuối (API lặp)
                    if not new_jobs:
                        break

                    for j in new_jobs:
                        seen_ids.add(str(j.get("id")))
                    all_jobs.extend(new_jobs)

                    # Nếu API trả về tổng số, dừng sớm khi đã lấy đủ
                    total = data.get("total")
                    per_page = data.get("per_page") or len(jobs)
                    if total is not None and per_page:
                        try:
                            if (page + 1) * int(per_page) >= int(total):
                                break
                        except (ValueError, TypeError):
                            pass

                return all_jobs
            except Exception as e:
                logger.error(f"Lỗi gọi Base API ({app_name}): {e}")
                return None


base_client = BaseClient()