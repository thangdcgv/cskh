import os
import time
import httpx
import logging
import asyncio
from typing import Dict, Optional, Any, List

from database import supabase

logger = logging.getLogger("uvicorn.error")
BASE_API_URL = "https://workflow.base.vn/extapi/v1"

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
                self._cache = {item["key"].strip(): str(item["value"]).strip() for item in res.data}
                self._last_updated = now
                return self._cache
        except Exception as e:
            logger.error(f"Lỗi tải system_settings: {e}")

        return {
            "base_repair_token": os.getenv("BASE_REPAIR_TOKEN", ""),
            "base_warranty_token": os.getenv("BASE_WARRANTY_TOKEN", ""),
            "base_recept_token": os.getenv("BASE_RECEPT_TOKEN", ""),
            "base_table_token": os.getenv("BASE_TABLE_TOKEN", ""),
            "workflow_repair_id": os.getenv("WORKFLOW_REPAIR_ID", "20025"),
        }

    async def get_token_and_wf(self, app_name: str = "repair") -> tuple[str, str]:
        settings = await self._get_settings()
        
        token_key = f"base_{app_name}_token"
        wf_key = f"workflow_{app_name}_id"
        wf_key_alt = f"workflow_{app_name}" # Thêm key dự phòng

        token = settings.get(token_key) or settings.get("base_shared_secret", "")
        wf_id = settings.get(wf_key) or settings.get(wf_key_alt, "")

        return token, wf_id

    async def fetch_jobs(self, app_name: str = "repair") -> Optional[List[dict]]:
        """Lấy danh sách tất cả Jobs trên Base. Trả về None nếu gặp lỗi kết nối/cấu hình."""
        token, wf_id = await self.get_token_and_wf(app_name)

        if not token or not wf_id:
            logger.error(f"Thiếu Token hoặc Workflow ID cho app: {app_name}")
            return None

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(
                    f"{BASE_API_URL}/jobs/get",
                    data={"access_token": token, "workflow_id": wf_id}
                )
                if res.status_code == 200:
                    data = res.json()
                    return data.get("jobs", [])
                else:
                    logger.error(f"Base API Error [{res.status_code}]: {res.text}")
                    return None
            except Exception as e:
                logger.error(f"Lỗi gọi Base API ({app_name}): {e}")
                return None

base_client = BaseClient()