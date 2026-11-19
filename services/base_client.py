import asyncio
import logging
import os
import time
from collections import Counter
from typing import Dict, List, Optional

import httpx

from database import supabase

logger = logging.getLogger("uvicorn.error")
BASE_API_URL = "https://workflow.base.vn/extapi/v1"

PAGE_LIMIT = 100
MAX_PAGES = 200          # 200 trang x 100 job = 20.000 job; vượt quá thì coi là bất thường
MAX_EMPTY_STREAK = 2     # Số trang liên tiếp không có job mới trước khi dừng (chịu được page 0 hoặc 1)
PAGE_RETRY_ATTEMPTS = 2  # Số lần thử lại mỗi trang nếu gặp lỗi mạng/5xx tạm thời


class BaseClient:
    def __init__(self):
        self._cache: Optional[Dict[str, str]] = None
        self._last_updated: float = 0
        self._default_ttl: float = 60.0

    def _get_env_defaults(self) -> Dict[str, str]:
        return {
            "base_repair_token": os.getenv("BASE_REPAIR_TOKEN", "").strip(),
            "base_storage_token": os.getenv("BASE_STORAGE_TOKEN", "").strip(),
            "base_delivery_token": os.getenv("BASE_DELIVERY_TOKEN", "").strip(),
            "base_warranty_token": os.getenv("BASE_WARRANTY_TOKEN", "").strip(),
            "base_recept_token": os.getenv("BASE_RECEPT_TOKEN", "").strip(),
            "base_table_token": os.getenv("BASE_TABLE_TOKEN", "").strip(),
            "workflow_repair_id": os.getenv("WORKFLOW_REPAIR_ID", "18190").strip(),
            "workflow_storage_id": os.getenv("WORKFLOW_STORAGE_ID", "18720").strip(),
            "workflow_delivery_id": os.getenv("WORKFLOW_DELIVERY_ID", "18249").strip(),
        }

    def _current_ttl(self) -> float:
        if self._cache and "cache_ttl_seconds" in self._cache:
            try:
                return max(5.0, float(self._cache["cache_ttl_seconds"]))
            except (ValueError, TypeError):
                pass
        return self._default_ttl

    async def _get_settings(self) -> Dict[str, str]:
        now = time.time()
        if self._cache and (now - self._last_updated) < self._current_ttl():
            return self._cache

        merged = self._get_env_defaults()

        try:
            res = await asyncio.to_thread(
                lambda: supabase.table("system_settings").select("key, value").execute()
            )
            if res.data:
                for item in res.data:
                    raw_key = item.get("key")
                    raw_val = item.get("value")
                    if raw_key is None or raw_val is None:
                        continue
                    key = str(raw_key).strip()
                    val = str(raw_val).strip()
                    if key and val and val.lower() != "none":
                        merged[key] = val

                self._cache = merged
                self._last_updated = now
                return self._cache
        except Exception as e:
            logger.error(f"Lỗi tải system_settings: {e}")
            if self._cache:
                return self._cache

        return merged

    async def get_token_and_wf(self, app_name: str = "repair") -> tuple[str, str]:
        app = (app_name or "repair").strip().lower()
        settings = await self._get_settings()

        token_key = f"base_{app}_token"
        wf_key = f"workflow_{app}_id"
        wf_key_alt = f"workflow_{app}"

        # Tuyệt đối không dùng base_shared_secret làm API Token
        token = settings.get(token_key, "").strip()
        wf_id = (settings.get(wf_key) or settings.get(wf_key_alt, "")).strip()

        return token, wf_id

    async def _post_page_with_retry(
        self, client: httpx.AsyncClient, url: str, form_data: dict, app_name: str, page: int
    ) -> Optional[dict]:
        """Gọi 1 trang Base API có retry nhẹ cho lỗi mạng hoặc lỗi 5xx."""
        for attempt in range(1, PAGE_RETRY_ATTEMPTS + 1):
            try:
                res = await client.post(url, data=form_data)
                if res.status_code == 200:
                    data = res.json()
                    if not isinstance(data, dict):
                        logger.error(
                            f"Base API trả về định dạng lạ ({app_name}, page {page}): {str(data)[:300]}"
                        )
                        return None
                    return data

                logger.warning(
                    f"Base API HTTP [{res.status_code}] ({app_name}, page {page}, lần {attempt}/{PAGE_RETRY_ATTEMPTS}): {res.text[:200]}"
                )
                # Lỗi 4xx (sai token, sai endpoint) thì dừng ngay, không retry
                if 400 <= res.status_code < 500:
                    return None
            except Exception as e:
                logger.warning(
                    f"Lỗi kết nối Base API ({app_name}, page {page}, lần {attempt}/{PAGE_RETRY_ATTEMPTS}): {e}"
                )

            if attempt < PAGE_RETRY_ATTEMPTS:
                await asyncio.sleep(0.6 * attempt)

        return None

    async def fetch_jobs(self, app_name: str = "repair") -> Optional[List[dict]]:
        """
        Lấy TOÀN BỘ Jobs của workflow trên Base (có phân trang).
        Chỉ trả về danh sách khi chắc chắn đã lấy xong; bất kỳ lỗi nào đều trả về None.
        """
        app = (app_name or "repair").strip().lower()
        token, wf_id = await self.get_token_and_wf(app)

        if not token or not wf_id:
            logger.error(f"Thiếu Token hoặc Workflow ID cho app: {app}")
            return None

        all_jobs: List[dict] = []
        seen_ids = set()
        page = 0
        empty_streak = 0

        async with httpx.AsyncClient(timeout=20.0) as client:
            while page < MAX_PAGES:
                data = await self._post_page_with_retry(
                    client=client,
                    url=f"{BASE_API_URL}/jobs/get",
                    form_data={
                        "access_token": token,
                        "workflow_id": wf_id,
                        "limit": PAGE_LIMIT,
                        "page": page,
                        "type": "all",
                    },
                    app_name=app,
                    page=page,
                )

                if data is None:
                    return None

                # Base.vn ExtAPI trả code = 1 khi thành công; code = 0 hoặc số âm là lỗi
                resp_code = data.get("code")
                if resp_code is not None and str(resp_code).strip() not in {"1", "200"}:
                    logger.error(f"Base API báo lỗi ({app}, page {page}): {str(data)[:300]}")
                    return None

                jobs = data.get("jobs") or []
                if isinstance(jobs, dict):
                    jobs = list(jobs.values())

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

                if new_count == 0:
                    empty_streak += 1
                    if empty_streak >= MAX_EMPTY_STREAK:
                        break
                else:
                    empty_streak = 0

                page += 1
            else:
                logger.error(f"Base API ({app}): vượt {MAX_PAGES} trang, hủy kết quả để an toàn")
                return None

        states = Counter(str(j.get("state") or "").strip().lower() for j in all_jobs)
        logger.info(
            f"[BASE] fetch_jobs({app}): {len(all_jobs)} job / {page + 1} trang | states={dict(states)}"
        )
        return all_jobs


base_client = BaseClient()