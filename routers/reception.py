import asyncio
import logging
import re
import urllib.parse
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from database import supabase
from websocket_manager import manager
from routers.auth import get_current_user, get_current_user_or_redirect

logger = logging.getLogger("uvicorn.error")

VN_TZ = timezone(timedelta(hours=7))
TABLE = "repairs"
MAX_CODE_RETRY = 5

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = (
    BASE_DIR.parent / "templates"
    if (BASE_DIR.parent / "templates").exists()
    else BASE_DIR / "templates"
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/api/reception", tags=["Reception"])


# ==================== SCHEMAS ====================
class ReceiveItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_name: str = Field(..., min_length=1, max_length=200, description="Tên khách hàng")
    phone: str = Field(..., min_length=8, max_length=20, description="Số điện thoại")
    address: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=1000)
    received_note: Optional[str] = Field(None, max_length=1000)
    base_job_id: Optional[str] = Field(None, max_length=100)


class UpdateItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_name: Optional[str] = Field(None, min_length=1, max_length=200)
    phone: Optional[str] = Field(None, min_length=8, max_length=20)
    address: Optional[str] = Field(None, max_length=500)
    note: Optional[str] = Field(None, max_length=1000)
    received_note: Optional[str] = Field(None, max_length=1000)
    status: Optional[str] = Field(None, description="Trạng thái đơn")


class ReturnItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[str] = Field(None, description="Trạng thái trả")
    received_note: Optional[str] = Field(None, max_length=1000)
    returned_note: Optional[str] = Field(None, max_length=1000)
    returned_method: Optional[str] = Field(None, max_length=100)
    is_fixed: Optional[bool] = Field(None, description="Đã sửa xong hay trả nguyên trạng")


# ==================== HELPERS ====================
def normalize_phone(phone: str) -> str:
    """Chuẩn hóa SĐT về dạng 0xxxxxxxxx"""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("84") and len(digits) == 11:
        return "0" + digits[2:]
    if len(digits) == 9 and digits.startswith("9"):
        return "0" + digits
    return digits if digits else phone


def escape_postgrest(value: str) -> str:
    """Escape ký tự đặc biệt trong filter or_ của PostgREST"""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def generate_sms_info(
    phone: str,
    customer_name: str,
    code: str,
    note: Optional[str],
    msg_type: str
) -> Optional[dict]:
    if not phone:
        return None
    phone_clean = normalize_phone(phone)
    now_str = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y")
    note_str = note.strip() if note and note.strip() else "Bình thường"

    if msg_type == "receive":
        sms_text = (
            f"May in Dai Thanh da tao phieu [{code}] ghi nhan thiet bi cua KH {customer_name} "
            f"luc {now_str}. Tinh trang: {note_str}."
        )
    else:
        sms_text = (
            f"May in Dai Thanh da hoan tra thiet bi [{code}] cho KH {customer_name} "
            f"luc {now_str}. Cam on Quy khach!"
        )

    encoded_text = urllib.parse.quote(sms_text)
    return {
        "phone": phone_clean,
        "text": sms_text,
        "sms_link": f"sms:{phone_clean}?body={encoded_text}",
        "sms_link_ios": f"sms:{phone_clean}&body={encoded_text}",
    }


async def get_next_code() -> tuple[str, int]:
    """Trả về (prefix, số tiếp theo) của mã phiếu trong ngày, dạng NT-MMDD-NNN"""
    now_vn = datetime.now(VN_TZ)
    prefix = f"NT-{now_vn.strftime('%m%d')}-"
    today_start_utc = (
        now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .isoformat()
    )
    query = (
        supabase.table(TABLE)
        .select("code")
        .gte("created_at", today_start_utc)
        .order("created_at", desc=True)
        .limit(1)
    )
    res = await asyncio.to_thread(query.execute)
    next_num = 1
    if res.data and res.data[0].get("code"):
        parts = res.data[0]["code"].split("-")
        if len(parts) >= 3:
            try:
                next_num = int(parts[-1]) + 1
            except (ValueError, IndexError):
                pass
    return prefix, next_num


async def safe_broadcast(message: dict) -> None:
    """Broadcast WebSocket an toàn"""
    try:
        await manager.broadcast(message)
    except Exception as e:
        logger.warning(f"WebSocket broadcast failed: {e}")


# ==================== TRANG GIAO DIỆN ====================
@router.get("/page", response_class=HTMLResponse)
def reception_page(request: Request, user: dict = Depends(get_current_user_or_redirect)):
    return templates.TemplateResponse(
        request=request,
        name="reception.html",
        context={"request": request, "current_user": user},
    )


# ==================== 1. TIẾP NHẬN MÁY ====================
@router.post("/receive")
async def create_reception_record(
    data: ReceiveItemRequest,
    user: dict = Depends(get_current_user),
):
    phone_clean = normalize_phone(data.phone)
    now_utc = datetime.now(timezone.utc).isoformat()

    record = None
    final_code = None

    for attempt in range(MAX_CODE_RETRY):
        prefix, next_num = await get_next_code()
        next_num += attempt
        final_code = f"{prefix}{next_num:03d}"

        payload = {
            "code": final_code,
            "customer_name": data.customer_name,
            "phone": phone_clean,
            "address": data.address,
            "note": data.note or "Chưa có yêu cầu",
            "received_note": data.received_note,
            "base_job_id": data.base_job_id,
            "status": "received",
            "current_process": "repair",
            "updated_at": now_utc,
        }

        try:
            insert_res = await asyncio.to_thread(
                supabase.table(TABLE).insert(payload).execute
            )
            if insert_res.data:
                record = insert_res.data[0]
                break
        except Exception as e:
            err_msg = str(e).lower()
            if "duplicate" in err_msg and "base_job_id" in err_msg:
                raise HTTPException(status_code=400, detail="Mã công việc (base_job_id) đã tồn tại")
            if "duplicate" in err_msg and attempt < MAX_CODE_RETRY - 1:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            logger.error(f"Lỗi insert phiếu tiếp nhận: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Không thể lưu phiếu, vui lòng thử lại")

    if not record:
        raise HTTPException(status_code=500, detail="Không thể tạo mã phiếu, vui lòng thử lại")

    await safe_broadcast({"type": "reception_new", "record": record})

    sms_info = generate_sms_info(
        phone=phone_clean,
        customer_name=data.customer_name,
        code=final_code,
        note=data.received_note,
        msg_type="receive",
    )

    return {
        "success": True,
        "message": "Đã tạo phiếu tiếp nhận!",
        "data": record,
        "sms_info": sms_info,
    }


# ==================== 2. TRA CỨU & LỌC CHUẨN XÁC ĐA QUY TRÌNH ====================
@router.get("/search")
async def search_reception_records(
    query_str: Optional[str] = Query(None, description="SĐT / Tên / Mã phiếu"),
    status: Optional[str] = Query("all", description="Lọc theo trạng thái hoặc quy trình"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    q = (query_str or "").strip()
    db_query = supabase.table(TABLE).select("*", count="exact")

    # 1. Tìm kiếm theo chuỗi từ khóa
    if q:
        q_esc = escape_postgrest(q)
        db_query = db_query.or_(
            f"phone.ilike.%{q_esc}%,customer_name.ilike.%{q_esc}%,code.ilike.%{q_esc}%"
        )

    # 2. Lọc theo trạng thái & quy trình đồng bộ chuẩn xác với Frontend
    if status and status.strip().lower() != "all":
        st = status.strip().lower()
        if st == "received":
            db_query = db_query.eq("status", "received").or_("current_process.eq.repair,current_process.is.null")
        elif st in ("processing", "repairing"):
            db_query = db_query.in_("status", ["processing", "repairing"]).or_("current_process.eq.repair,current_process.is.null")
        elif st == "storage":
            db_query = db_query.eq("current_process", "storage").neq("storage_status", "dispatched")
        elif st == "delivery":
            db_query = db_query.eq("current_process", "delivery").neq("delivery_status", "delivered")
        elif st == "fixed":
            db_query = db_query.eq("status", "fixed").or_("current_process.eq.repair,current_process.is.null")
        elif st == "returned":
            db_query = db_query.or_("status.eq.returned,storage_status.eq.dispatched,delivery_status.eq.delivered")
        elif st == "cancelled":
            db_query = db_query.eq("status", "cancelled")
        else:
            db_query = db_query.eq("status", st)

    start = (page - 1) * limit
    end = start + limit - 1

    try:
        res = await asyncio.to_thread(
            lambda: db_query.order("created_at", desc=True).range(start, end).execute()
        )
    except Exception as e:
        logger.error(f"Lỗi tra cứu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi tra cứu dữ liệu")

    total = res.count if res.count is not None else len(res.data or [])
    total_pages = (total + limit - 1) // limit if total > 0 else 1

    return {
        "success": True,
        "data": res.data or [],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
        },
    }


# ==================== 3. LẤY CHI TIẾT PHIẾU ====================
@router.get("/{record_id}")
async def get_reception_record(
    record_id: str,
    user: dict = Depends(get_current_user),
):
    try:
        UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    try:
        res = await asyncio.to_thread(
            supabase.table(TABLE).select("*").eq("id", record_id).limit(1).execute
        )
    except Exception as e:
        logger.error(f"Lỗi lấy chi tiết phiếu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")
    return {"success": True, "data": res.data[0]}


# ==================== 4. CẬP NHẬT PHIẾU (SỬA) ====================
@router.put("/{record_id}")
async def update_reception_record(
    record_id: str,
    data: UpdateItemRequest,
    user: dict = Depends(get_current_user),
):
    try:
        UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    now_utc = datetime.now(timezone.utc).isoformat()
    update_payload = {"updated_at": now_utc}

    if data.customer_name is not None:
        update_payload["customer_name"] = data.customer_name
    if data.phone is not None:
        update_payload["phone"] = normalize_phone(data.phone)
    if data.address is not None:
        update_payload["address"] = data.address
    if data.note is not None:
        update_payload["note"] = data.note
    if data.received_note is not None:
        update_payload["received_note"] = data.received_note
    if data.status is not None:
        st_val = str(data.status).strip().lower()
        update_payload["status"] = st_val
        if st_val == "returned":
            update_payload["returned_date"] = now_utc

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE).update(update_payload).eq("id", record_id).execute()
        )
    except Exception as e:
        logger.error(f"Lỗi cập nhật phiếu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Không thể cập nhật thông tin phiếu")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")

    updated_record = res.data[0]
    await safe_broadcast({"type": "reception_updated", "record": updated_record})

    return {
        "success": True,
        "message": "Đã cập nhật thông tin phiếu thành công!",
        "data": updated_record,
    }


# ==================== 5. TRẢ MÁY CHO KHÁCH ====================
@router.post("/{record_id}/return")
@router.post("/return/{record_id}")
async def return_item_to_customer(
    record_id: str,
    data: Optional[ReturnItemRequest] = None,
    user: dict = Depends(get_current_user),
):
    try:
        UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    try:
        existing = await asyncio.to_thread(
            supabase.table(TABLE).select("*").eq("id", record_id).limit(1).execute
        )
    except Exception as e:
        logger.error(f"Lỗi truy vấn phiếu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống")

    if not existing.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu nhận hàng")

    record = existing.data[0]
    current_status = str(record.get("status") or "").lower()
    current_process = str(record.get("current_process") or "repair").lower()

    # Ràng buộc 1: Nếu thuộc Quy trình Giao Nhận -> KHÔNG ĐƯỢC TRẢ MÁY TẠI QUẦY
    if current_process == "delivery":
        raise HTTPException(
            status_code=400,
            detail="Đơn hàng đang thuộc Quy trình Giao nhận! Việc bàn giao do Shipper/Bên Giao nhận thực hiện."
        )

    # Ràng buộc 2: Nếu đang sửa chữa -> KHÔNG ĐƯỢC TRẢ MÁY
    if current_process == "repair" and current_status in ("processing", "repairing"):
        raise HTTPException(
            status_code=400,
            detail="Thiết bị đang trong quá trình sửa chữa! KTV cần hoàn thành hoặc báo hủy sửa trước khi trả máy."
        )

    # Ràng buộc 3: Đã trả rồi -> Báo lỗi
    if current_status == "returned":
        raise HTTPException(status_code=400, detail="Phiếu này đã được xác nhận trả trước đó.")

    now_utc = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": "returned",
        "returned_date": now_utc,
        "updated_at": now_utc,
    }

    if data:
        if data.returned_note:
            payload["returned_note"] = data.returned_note
        if data.returned_method:
            payload["returned_method"] = data.returned_method
        if data.received_note:
            payload["received_note"] = data.received_note
        if data.is_fixed is not None:
            payload["is_fixed"] = data.is_fixed

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE).update(payload).eq("id", record_id).execute()
        )
    except Exception as e:
        logger.error(f"Lỗi trả máy: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Không thể cập nhật trạng thái trả máy")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu nhận hàng")

    updated = res.data[0]
    await safe_broadcast({"type": "reception_returned", "record": updated})

    sms_info = generate_sms_info(
        phone=updated.get("phone"),
        customer_name=updated.get("customer_name"),
        code=updated.get("code"),
        note=data.returned_note if data else None,
        msg_type="return",
    )

    return {
        "success": True,
        "message": "Đã ghi nhận trả máy thành công!",
        "data": updated,
        "sms_info": sms_info,
    }