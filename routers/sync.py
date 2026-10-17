import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List
from fastapi import APIRouter, HTTPException, status
from database import supabase
from services.base_client import base_client

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/sync", tags=["Sync"])

# =========================================================
# NGƯỠNG AN TOÀN — CHỐNG HỦY HÀNG LOẠT DO BASE API BẤT THƯỜNG
# =========================================================
# Số phiếu tối đa được phép hủy trong một lần chạy. Vượt ngưỡng -> BỎ QUA, log cảnh báo.
MAX_CANCEL_PER_RUN = 20
# Phiếu mới tạo dưới 5 phút sẽ bị bỏ qua (phòng lag Base API chưa trả job vừa tạo).
MIN_TICKET_AGE_MINUTES = 5


@router.post("/reconcile")
async def reconcile_tickets(app_name: str = "repair"):
    """Đối soát dữ liệu giữa Base.vn và Web App theo đúng ứng dụng/quy trình."""
    try:
        # 1. Lấy danh sách Jobs từ Base
        base_jobs = await base_client.fetch_jobs(app_name)

        if base_jobs is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Không thể kết nối đến Base API ({app_name}) để lấy danh sách công việc"
            )

        # 2. Lọc job hợp lệ (bỏ xóa/lưu trữ/hủy)
        valid_base_job_ids = set()
        for j in base_jobs:
            if not j.get("id"):
                continue
            job_state = str(j.get("state") or "").strip().lower()
            is_archive = str(j.get("is_archive") or "0").strip().lower()
            if is_archive in {"1", "true"} or job_state in {"deleted", "cancelled", "archived", "deleted_job"}:
                continue
            valid_base_job_ids.add(str(j.get("id")).strip())

        # 🛡️ BẢO VỆ 1: Base trả 0 job hợp lệ -> CỨNG NHẮC không hủy phiếu nào.
        # Tránh token sai / workflow_id sai / API lỗi trả rỗng -> hủy toàn bộ dữ liệu.
        if not valid_base_job_ids:
            logger.warning(
                f"🛑 [RECONCILE-{app_name.upper()}] Base trả 0 job hợp lệ "
                f"(gốc {len(base_jobs)}). Dừng, không hủy phiếu nào. Kiểm tra token/workflow_id."
            )
            return {
                "status": "skipped",
                "app_name": app_name,
                "checked_total": 0,
                "cleaned_count": 0,
                "message": f"Base trả về danh sách rỗng ({app_name}). Đã dừng để tránh hủy hàng loạt."
            }

        # 3. Lấy phiếu đang hoạt động, BỎ QUA phiếu đang trong thời gian tạm khóa đối soát
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()
        # Dùng định dạng Z trong query để tránh lỗi mã hóa ký tự '+'
        now_query = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        min_created_at = (now_dt - timedelta(minutes=MIN_TICKET_AGE_MINUTES)).isoformat()

        def _fetch_active_tickets():
            return (
                supabase.table("repairs")
                .select("id, code, base_job_id, storage_job_id, delivery_job_id, current_process, skip_reconcile_until, created_at")
                .not_.in_("status", ["returned", "cancelled"])
                .or_(f"skip_reconcile_until.is.null,skip_reconcile_until.lt.{now_query}")
                .execute()
            )

        db_res = await asyncio.to_thread(_fetch_active_tickets)
        db_tickets = db_res.data or []

        if not db_tickets:
            return {
                "status": "success",
                "message": "Không có phiếu nào đang hoạt động cần đối soát.",
                "cleaned_count": 0,
            }

        # 4. Lọc phiếu thuộc đúng quy trình
        ids_to_cancel: List[int] = []
        target_process = app_name.lower()

        for ticket in db_tickets:
            curr_proc = str(ticket.get("current_process") or "").strip().lower()

            # 🛡️ BẢO VỆ 2 (thứ cấp): phiếu mới tạo dưới 5 phút -> bỏ qua
            ticket_created = str(ticket.get("created_at") or "").strip()
            if ticket_created and ticket_created > min_created_at:
                continue

            if target_process == "storage":
                if curr_proc != "storage":
                    continue
                target_job_id = str(ticket.get("storage_job_id") or "").strip()
            elif target_process == "delivery":
                if curr_proc != "delivery":
                    continue
                target_job_id = str(ticket.get("delivery_job_id") or "").strip()
            else:
                if curr_proc in {"storage", "delivery"}:
                    continue
                target_job_id = str(ticket.get("base_job_id") or "").strip()

            if target_job_id and target_job_id not in valid_base_job_ids:
                ids_to_cancel.append(ticket["id"])
                logger.info(
                    f"🔍 [RECONCILE-{app_name.upper()}] Phiếu {ticket.get('code')} "
                    f"(id={ticket['id']}) job_id={target_job_id} không tồn tại trên Base."
                )

        # 🛡️ BẢO VỆ 3: quá nhiều phiếu cần hủy -> rất có thể API bất thường -> dừng
        if len(ids_to_cancel) > MAX_CANCEL_PER_RUN:
            logger.warning(
                f"🛑 [RECONCILE-{app_name.upper()}] Cần hủy {len(ids_to_cancel)} phiếu, "
                f"vượt ngưỡng {MAX_CANCEL_PER_RUN}. Bỏ qua, kiểm tra thủ công. "
                f"IDs: {ids_to_cancel[:10]}{'...' if len(ids_to_cancel) > 10 else ''}"
            )
            return {
                "status": "skipped",
                "app_name": app_name,
                "checked_total": len(db_tickets),
                "cleaned_count": 0,
                "message": f"Phát hiện {len(ids_to_cancel)} phiếu cần hủy, vượt ngưỡng an toàn {MAX_CANCEL_PER_RUN}. Đã dừng."
            }

        # 5. Batch update hủy phiếu rác
        cleaned_count = len(ids_to_cancel)
        if ids_to_cancel:
            def _batch_update_cancel():
                return (
                    supabase.table("repairs")
                    .update({
                        "status": "cancelled",
                        "failure_reason": f"Công việc đã bị xóa khỏi Base Workflow ({app_name})",
                        "updated_at": now_iso,
                    })
                    .in_("id", ids_to_cancel)
                    .execute()
                )

            await asyncio.to_thread(_batch_update_cancel)
            logger.info(f"🧹 [RECONCILE-{app_name.upper()}] Đã dọn {cleaned_count} phiếu rác: {ids_to_cancel}")

        return {
            "status": "success",
            "app_name": app_name,
            "checked_total": len(db_tickets),
            "cleaned_count": cleaned_count,
            "message": f"Đối soát [{app_name}] thành công. Đã làm sạch {cleaned_count} phiếu rác.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Sync Error] Lỗi đối soát dữ liệu với Base: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi hệ thống khi đối soát: {str(e)}"
        )