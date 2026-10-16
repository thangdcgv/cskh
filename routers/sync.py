import asyncio
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException, status
from database import supabase
from services.base_client import base_client

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/sync", tags=["Sync"])


@router.post("/reconcile")
async def reconcile_tickets(app_name: str = "repair"):
    """
    Đối soát dữ liệu giữa Base.vn và Web App theo đúng ứng dụng/quy trình.
    """
    try:
        # 1. Lấy danh sách Jobs của quy trình hiện tại từ Base
        base_jobs = await base_client.fetch_jobs(app_name)
        
        # An toàn tuyệt đối: Nếu lỗi API (base_jobs is None) thì ngừng ngay, không xóa dữ liệu
        if base_jobs is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Không thể kết nối đến Base API ({app_name}) để lấy danh sách công việc"
            )

        # ------------------------------------------------------------------
        # ĐOẠN ĐÃ FIX: LỌC BỎ CÁC JOB ĐÃ BỊ XÓA/LƯU TRỮ NGẦM TRÊN BASE
        # ------------------------------------------------------------------
        valid_base_job_ids = set()
        for j in base_jobs:
            if not j.get("id"):
                continue
            
            state = str(j.get("state") or "").strip().lower()
            is_archive = str(j.get("is_archive") or "0").strip()
            
            # Bỏ qua các job bị xóa hoặc lưu trữ
            if is_archive in ["1", "true", "True"] or state in ["deleted", "cancelled", "archived", "deleted_job"]:
                continue
                
            valid_base_job_ids.add(str(j.get("id")).strip())
        # ------------------------------------------------------------------

        # 2. Lấy phiếu đang hoạt động dưới App
        def _fetch_active_tickets():
            return supabase.table("repairs")\
                .select("id, code, base_job_id, storage_job_id, delivery_job_id, current_process")\
                .not_.in_("status", ["returned", "cancelled"])\
                .execute()

        db_res = await asyncio.to_thread(_fetch_active_tickets)
        db_tickets = db_res.data or []

        if not db_tickets:
            return {
                "status": "success",
                "message": "Không có phiếu nào đang hoạt động cần đối soát.",
                "cleaned_count": 0
            }

        # 3. CHỈ đối soát những phiếu thuộc đúng quy trình app_name đang gọi
        ids_to_cancel: List[int] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        target_process = app_name.lower()

        for ticket in db_tickets:
            curr_proc = str(ticket.get("current_process") or "").strip().lower()
            
            # Logic map linh hoạt để không bỏ sót phiếu rỗng current_process
            if target_process == "storage":
                if curr_proc != "storage":
                    continue
                target_job_id = str(ticket.get("storage_job_id") or "").strip()
            elif target_process == "delivery":
                if curr_proc != "delivery":
                    continue
                target_job_id = str(ticket.get("delivery_job_id") or "").strip()
            else:
                # Mặc định là Repair. Nếu phiếu đang ở Storage/Delivery thì bỏ qua.
                if curr_proc in ["storage", "delivery"]:
                    continue
                target_job_id = str(ticket.get("base_job_id") or "").strip()

            # Nếu phiếu có Job ID nhưng ID đó không còn tồn tại trên Workflow tương ứng
            if target_job_id and target_job_id not in valid_base_job_ids:
                ids_to_cancel.append(ticket["id"])

        # 4. Batch Update hủy phiếu rác
        cleaned_count = len(ids_to_cancel)
        if ids_to_cancel:
            def _batch_update_cancel():
                return supabase.table("repairs").update({
                    "status": "cancelled",
                    "failure_reason": f"Công việc đã bị xóa khỏi Base Workflow ({app_name})",
                    "updated_at": now_iso
                }).in_("id", ids_to_cancel).execute()

            await asyncio.to_thread(_batch_update_cancel)
            logger.info(f"🧹 [RECONCILE-{app_name.upper()}] Đã dọn dẹp {cleaned_count} phiếu rác: {ids_to_cancel}")

        return {
            "status": "success",
            "app_name": app_name,
            "checked_total": len(db_tickets),
            "cleaned_count": cleaned_count,
            "message": f"Đối soát [{app_name}] thành công. Đã làm sạch {cleaned_count} phiếu rác."
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Sync Error] Lỗi đối soát dữ liệu với Base: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi hệ thống khi đối soát: {str(e)}"
        )