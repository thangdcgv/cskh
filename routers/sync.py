import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from database import supabase
from routers.auth import get_current_user
from services.base_client import base_client
from utils import extract_identifier_code_or_phone

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/sync", tags=["Sync"])

VALID_APPS = {"repair", "storage", "delivery"}
INACTIVE_STATUSES = ["returned", "cancelled"]
ARCHIVED_STATES = {"deleted", "cancelled", "archived", "deleted_job"}

# Phiếu vừa được webhook cập nhật trong khoảng này sẽ KHÔNG bị đối soát đụng tới.
GRACE_PERIOD = timedelta(minutes=10)

# Chặn hủy hàng loạt nếu danh sách Base trả về bất thường
MAX_CANCEL_ABSOLUTE = 5
MAX_CANCEL_RATIO = 0.3

UPDATE_CHUNK_SIZE = 100

# Khóa chống chạy đối soát trùng lặp đồng thời trên cùng một quy trình
_reconcile_locks: Dict[str, asyncio.Lock] = {app: asyncio.Lock() for app in VALID_APPS}


def _is_recent(ts, now: datetime) -> bool:
    """True nếu mốc thời gian nằm trong GRACE_PERIOD. Không đọc được thời gian -> coi là mới (an toàn)."""
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt) < GRACE_PERIOD
    except Exception:
        return True


def _job_id_column(app_name: str) -> str:
    if app_name == "storage":
        return "storage_job_id"
    if app_name == "delivery":
        return "delivery_job_id"
    return "base_job_id"


def _target_job_id(ticket: dict, app_name: str):
    """Trả về job_id của phiếu trong đúng quy trình app_name, hoặc None nếu phiếu không thuộc quy trình đó."""
    curr_proc = str(ticket.get("current_process") or "").strip().lower()

    if app_name == "storage":
        if curr_proc != "storage":
            return None
        return str(ticket.get("storage_job_id") or "").strip() or None

    if app_name == "delivery":
        if curr_proc != "delivery":
            return None
        return str(ticket.get("delivery_job_id") or "").strip() or None

    # Mặc định là repair. Phiếu đang ở Storage/Delivery thì bỏ qua.
    if curr_proc in {"storage", "delivery"}:
        return None
    return str(ticket.get("base_job_id") or "").strip() or None


async def _reconcile_core(app_name: str) -> dict:
    app_name = (app_name or "repair").strip().lower()
    if app_name not in VALID_APPS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"app_name không hợp lệ: {app_name}"
        )

    lock = _reconcile_locks[app_name]
    if lock.locked():
        return {
            "status": "skipped",
            "app_name": app_name,
            "cleaned_count": 0,
            "message": f"Tiến trình đối soát [{app_name}] đang chạy, bỏ qua lượt gọi trùng.",
        }

    async with lock:
        # 1. Lấy danh sách Jobs của quy trình hiện tại từ Base
        base_jobs = await base_client.fetch_jobs(app_name)

        if base_jobs is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Không thể kết nối đến Base API ({app_name}) để lấy danh sách công việc"
            )

        # Tập job còn "sống" trên Base (bỏ job đã xóa / lưu trữ)
        valid_base_job_ids = set()
        alive_code_to_job_id: Dict[str, str] = {}
        raw_jobs_by_id = {}

        for j in base_jobs:
            job_id = str(j.get("id") or "").strip()
            if not job_id:
                continue
            raw_jobs_by_id[job_id] = j
            state = str(j.get("state") or "").strip().lower()
            is_archive = str(j.get("is_archive") or "0").strip().lower()
            if is_archive in {"1", "true"} or state in ARCHIVED_STATES:
                continue
            valid_base_job_ids.add(job_id)

            job_name = str(j.get("name") or j.get("title") or "")
            try:
                code_in_name, _ = extract_identifier_code_or_phone(job_name)
            except Exception:
                code_in_name = None
            if code_in_name:
                alive_code_to_job_id[str(code_in_name).strip().lower()] = job_id

        # 2. Lấy phiếu đang hoạt động dưới App
        def _fetch_active_tickets():
            return (
                supabase.table("repairs")
                .select("id, code, status, base_job_id, storage_job_id, delivery_job_id, current_process, updated_at")
                .not_.in_("status", INACTIVE_STATUSES)
                .execute()
            )

        db_res = await asyncio.to_thread(_fetch_active_tickets)
        db_tickets = db_res.data or []

        if not db_tickets:
            return {
                "status": "success",
                "app_name": app_name,
                "message": "Không có phiếu nào đang hoạt động cần đối soát.",
                "cleaned_count": 0,
            }

        # 3. Chỉ đối soát phiếu thuộc đúng quy trình app_name
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        job_col = _job_id_column(app_name)

        checked = 0
        skipped_recent = 0
        relinked_count = 0
        missing: List[dict] = []

        for ticket in db_tickets:
            target_job_id = _target_job_id(ticket, app_name)
            if not target_job_id:
                continue

            checked += 1

            if target_job_id in valid_base_job_ids:
                continue

            # Job cũ không còn nhưng Base đã tạo job mới mang cùng mã phiếu (chuyển ngược quy trình)
            # -> giữ nguyên phiếu và tự cập nhật lại job_id mới
            ticket_code_norm = str(ticket.get("code") or "").strip().lower()
            if ticket_code_norm and ticket_code_norm in alive_code_to_job_id:
                new_alive_job_id = alive_code_to_job_id[ticket_code_norm]
                logger.info(
                    f"🔁 [RECONCILE-{app_name.upper()}] Phiếu {ticket.get('code')}: cập nhật {job_col} "
                    f"'{target_job_id}' -> '{new_alive_job_id}'"
                )
                try:
                    await asyncio.to_thread(
                        lambda tid=ticket["id"], jid=new_alive_job_id: supabase.table("repairs")
                        .update({job_col: jid, "updated_at": now_iso})
                        .eq("id", tid)
                        .execute()
                    )
                    relinked_count += 1
                except Exception as relink_err:
                    logger.warning(f"Không thể cập nhật lại {job_col} cho phiếu {ticket.get('code')}: {relink_err}")
                continue

            # Phiếu vừa được webhook cập nhật -> có thể Base chưa kịp đồng bộ danh sách, bỏ qua
            if _is_recent(ticket.get("updated_at"), now):
                skipped_recent += 1
                continue

            raw = raw_jobs_by_id.get(target_job_id)
            if raw is not None:
                detail = f"CÓ trong danh sách nhưng bị lọc (state={raw.get('state')!r}, is_archive={raw.get('is_archive')!r})"
            else:
                detail = f"KHÔNG có trong danh sách Base ({len(base_jobs)} job)"
            logger.warning(
                f"🔎 [RECONCILE-{app_name.upper()}] Phiếu {ticket.get('code')} (job {target_job_id}) {detail}"
            )

            missing.append(ticket)

        # 3a. Base không trả về job hợp lệ nào nhưng DB có phiếu -> bỏ qua để tránh hủy nhầm
        if checked > 0 and not valid_base_job_ids:
            logger.warning(
                f"⚠️ [RECONCILE-{app_name.upper()}] Base trả về 0 job hợp lệ nhưng DB có {checked} phiếu. "
                f"Bỏ qua đối soát để tránh hủy nhầm."
            )
            return {
                "status": "skipped",
                "app_name": app_name,
                "checked_total": checked,
                "cleaned_count": 0,
                "message": "Base không trả về job hợp lệ nào, bỏ qua đối soát để an toàn.",
            }

        # 3b. Chặn hủy hàng loạt
        max_allowed = max(MAX_CANCEL_ABSOLUTE, int(checked * MAX_CANCEL_RATIO))
        if len(missing) > max_allowed:
            logger.warning(
                f"⚠️ [RECONCILE-{app_name.upper()}] Định hủy {len(missing)}/{checked} phiếu (ngưỡng {max_allowed}). "
                f"Nghi danh sách Base bị thiếu (phân trang/lọc stage). Không hủy phiếu nào. "
                f"Mã phiếu: {[t.get('code') for t in missing]}"
            )
            return {
                "status": "skipped",
                "app_name": app_name,
                "checked_total": checked,
                "cleaned_count": 0,
                "message": f"Số phiếu định hủy ({len(missing)}) vượt ngưỡng an toàn ({max_allowed}), đã bỏ qua.",
            }

        # 4. Hủy phiếu rác theo lô (đồng bộ đầy đủ trường trạng thái với webhook.py)
        ids_to_cancel = [t["id"] for t in missing]
        cleaned_count = len(ids_to_cancel)

        if ids_to_cancel:
            reason = f"Công việc đã bị xóa khỏi Base Workflow ({app_name})"

            def _cancel_chunk(chunk: list):
                return (
                    supabase.table("repairs")
                    .update({
                        "status": "cancelled",
                        "is_fixed": False,
                        "repair_result": "failed",
                        "failure_reason": reason,
                        "updated_at": now_iso,
                    })
                    .in_("id", chunk)
                    .not_.in_("status", INACTIVE_STATUSES)
                    .execute()
                )

            for i in range(0, len(ids_to_cancel), UPDATE_CHUNK_SIZE):
                chunk = ids_to_cancel[i:i + UPDATE_CHUNK_SIZE]
                await asyncio.to_thread(_cancel_chunk, chunk)

            logger.info(
                f"🧹 [RECONCILE-{app_name.upper()}] Đã dọn dẹp {cleaned_count} phiếu rác: "
                f"{[t.get('code') for t in missing]}"
            )

        return {
            "status": "success",
            "app_name": app_name,
            "checked_total": checked,
            "skipped_recent": skipped_recent,
            "relinked_count": relinked_count,
            "cleaned_count": cleaned_count,
            "message": f"Đối soát [{app_name}] thành công. Đã làm sạch {cleaned_count} phiếu rác.",
        }


@router.post("/reconcile", dependencies=[Depends(get_current_user)])
async def reconcile_tickets(app_name: str = "repair"):
    """
    Đối soát dữ liệu giữa Base.vn và Web App theo đúng ứng dụng/quy trình.
    Giữ nguyên tên và chữ ký để webhook (safe_background_reconcile) vẫn import/gọi trực tiếp được.
    """
    try:
        return await _reconcile_core(app_name)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Sync Error] Lỗi đối soát dữ liệu với Base: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi đối soát"
        )