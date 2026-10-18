import os
import time
import httpx
import logging
import asyncio
from collections import Counter
from typing import Dict, Optional, List

from database import supabase

logger = logging.getLogger("uvicorn.error")
BASE_API_URL = "https://workflow.base.vn/extapi/v1"

PAGE_LIMIT = 100
MAX_PAGES = 200          # 200 trang x 100 job = 20.000 job; vượt quá thì coi là bất thường
MAX_EMPTY_STREAK = 2     # số trang liên tiếp không có job mới trước khi dừng (chịu được page bắt đầu từ 0 hoặc 1)


class BaseClient:
    def __init__(self):
        self._cache: Optional[Dict[str, str]] = None
        self._last_updated: float = 0
        self._ttl: float = 60.0  # Cache 60s

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
                    if item.get("key") is not None
                }
                self._last_updated = now
                return self._cache
        except Exception as e:
            logger.error(f"Lỗi tải system_settings: {e}")
            # Ưu tiên dùng cache cũ (đầy đủ key) thay vì rơi xuống giá trị env thiếu key
            if self._cache:
                return self._cache

        # Fallback mặc định nếu mất kết nối DB và chưa có cache
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

        token_key = f"base_{app_name}_token"
        wf_key = f"workflow_{app_name}_id"
        wf_key_alt = f"workflow_{app_name}"  # Key dự phòng

        # Tuyệt đối không dùng base_shared_secret làm API Token
        token = settings.get(token_key, "").strip()
        wf_id = (settings.get(wf_key) or settings.get(wf_key_alt, "")).strip()

        return token, wf_id

    async def fetch_jobs(self, app_name: str = "repair") -> Optional[List[dict]]:
        """
        Lấy TOÀN BỘ Jobs của workflow trên Base (có phân trang).

        Quy tắc an toàn: chỉ trả về danh sách khi chắc chắn đã lấy xong.
        Bất kỳ lỗi nào (HTTP, JSON, body báo lỗi, quá nhiều trang) -> trả None,
        KHÔNG BAO GIỜ trả danh sách dở dang, vì reconcile sẽ coi job vắng mặt là "đã bị xóa".
        """
        token, wf_id = await self.get_token_and_wf(app_name)

        if not token or not wf_id:
            logger.error(f"Thiếu Token hoặc Workflow ID cho app: {app_name}")
            return None

        all_jobs: List[dict] = []
        seen_ids = set()
        page = 0
        empty_streak = 0

        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                while page < MAX_PAGES:
                    res = await client.post(
                        f"{BASE_API_URL}/jobs/get",
                        data={
                            "access_token": token,
                            "workflow_id": wf_id,
                            "limit": PAGE_LIMIT,
                            "page": page,
                            # CẦN KIỂM CHỨNG với tài liệu Base: tham số này có thật sự
                            # trả cả job Done/Forwarded/Failed hay không
                            "type": "all",
                        },
                    )

                    if res.status_code != 200:
                        logger.error(f"Base API Error [{res.status_code}] ({app_name}, page {page}): {res.text[:300]}")
                        return None

                    data = res.json()
                    if not isinstance(data, dict):
                        logger.error(f"Base API trả về định dạng lạ ({app_name}, page {page}): {str(data)[:300]}")
                        return None

                    # HTTP 200 nhưng body báo lỗi (token sai, hết quyền...). Kiểm tra lại quy ước code với docs Base.
                    if str(data.get("code")) == "0":
                        logger.error(f"Base API báo lỗi ({app_name}, page {page}): {str(data)[:300]}")
                        return None

                    jobs = data.get("jobs") or []
                    if isinstance(jobs, dict):
                        jobs = list(jobs.values())

                    # Trang rỗng = hết dữ liệu
                    if not jobs:
                        break

                    new_count = 0
                    for j in jobs:
                        if not isinstance(j, dict):
                            continue
                        jid = str(j.get("id") or "").strip()
                        if not jid or jid in seen_ids:
                            continue
                        seen_ids.add(jid)
                        all_jobs.append(j)
                        new_count += 1

                    # Trang không có job mới (Base lặp trang hoặc bỏ qua tham số page)
                    if new_count == 0:
                        empty_streak += 1
                        if empty_streak >= MAX_EMPTY_STREAK:
                            break
                    else:
                        empty_streak = 0

                    page += 1
                else:
                    # Thoát vì vượt MAX_PAGES chứ không phải vì hết dữ liệu -> không tin cậy
                    logger.error(f"Base API ({app_name}): vượt {MAX_PAGES} trang, hủy kết quả để an toàn")
                    return None

            except Exception as e:
                logger.error(f"Lỗi gọi Base API ({app_name}): {e}")
                return None

        states = Counter(str(j.get("state") or "").strip().lower() for j in all_jobs)
        logger.info(
            f"[BASE] fetch_jobs({app_name}): {len(all_jobs)} job / {page + 1} trang | states={dict(states)}"
        )
        return all_jobs


base_client = BaseClient()