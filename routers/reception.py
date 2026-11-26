import asyncio
import logging
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from database import settings, supabase, supabase_admin
from routers.auth import get_current_user, get_current_user_or_redirect
from routers.permissions import can_access, can_modify_warranty_record
from templates_config import templates
from websocket_manager import manager

logger = logging.getLogger("uvicorn.error")
VN_TZ = timezone(timedelta(hours=7))
TABLE = "repairs"
MAX_CODE_RETRY = 5
ALLOWED_STATUSES = {"received", "processing", "fixed", "returned", "cancelled"}

router = APIRouter(prefix="/api/reception", tags=["Reception"])

_settings_cache = {"data": {}, "expires_at": 0}


# ==================== SCHEMAS ====================
class ReceiveItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    customer_name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=8, max_length=20)
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
    status: Optional[str] = None


class ReturnItemRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    received_note: Optional[str] = Field(None, max_length=1000)
    returned_note: Optional[str] = Field(None, max_length=1000)
    returned_method: Optional[str] = Field(None, max_length=100)
    is_fixed: Optional[bool] = None


# ==================== HELPERS ====================
def normalize_phone(phone: str) -> str:
    """Chuẩn hóa SĐT về dạng 0xxxxxxxxx (chỉ số, 10 ký tự) — khớp CHECK constraint"""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("84") and len(digits) == 11:
        return "0" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        return digits
    if len(digits) == 9 and digits[0] in "35789":
        return "0" + digits
    return digits[:11] if digits else ""


def escape_postgrest(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
        .replace(",", "\\,")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


EMPTY_NOTE_VALUES = {
    "",
    "chưa ghi chú",
    "chua ghi chu",
    "chưa ghi nhận",
    "chua ghi nhan",
    "bình thường",
    "binh thuong",
    "no value",
    "null",
    "none",
}


def _to_ascii_sms(text: str) -> str:
    """Chuyển tiếng Việt có dấu sang không dấu để SMS giữ chuẩn 160 ký tự/tin."""
    if not text:
        return ""
    text = text.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _get_sms_settings() -> dict:
    now = time.time()
    if now < _settings_cache["expires_at"]:
        return _settings_cache["data"]
    try:
        res = (
            supabase_admin.table("system_settings")
            .select("key, value")
            .in_("key", ["sms_hotline", "tracking_domain", "company_name"])
            .execute()
        )
        data = {item["key"]: item["value"] for item in (res.data or [])}
    except Exception as e:
        logger.warning(f"Không đọc được cấu hình SMS: {e}")
        data = {}
    data.setdefault("sms_hotline", "0938580406")
    data.setdefault("tracking_domain", "cskh-nu.vercel.app")
    data.setdefault("company_name", "May in Dai Thanh")
    _settings_cache["data"] = data
    _settings_cache["expires_at"] = now + 60
    return data


def generate_sms_info(phone, customer_name, code, note, msg_type) -> Optional[dict]:
    if not phone:
        return None

    phone_clean = normalize_phone(phone)
    if not phone_clean:
        return None

    cfg = _get_sms_settings()
    hotline = cfg["sms_hotline"]
    company = _to_ascii_sms(cfg["company_name"])
    name_clean = _to_ascii_sms((customer_name or "Khach le").strip())
    now_str = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y")

    raw_note = (note or "").strip()
    has_real_note = raw_note.lower() not in EMPTY_NOTE_VALUES
    note_segment = f" Ghi chu: {_to_ascii_sms(raw_note)}." if has_real_note else ""

    if msg_type == "receive":
        sms_text = (
            f"{company} tao phieu [{code}] tiep nhan thiet bi KH {name_clean} "
            f"luc {now_str}.{note_segment} LH: {hotline}"
        )
    else:
        sms_text = (
            f"{company} da hoan tra thiet bi [{code}] cho KH {name_clean} "
            f"luc {now_str}. Cam on Quy khach! LH: {hotline}"
        )

    encoded_text = urllib.parse.quote(sms_text)
    return {
        "phone": phone_clean,
        "text": sms_text,
        "sms_link": f"sms:{phone_clean}?body={encoded_text}",
        "sms_link_ios": f"sms:{phone_clean}&body={encoded_text}",
    }


async def get_next_code() -> tuple[str, int]:
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
        .like("code", f"{prefix}%")
        .order("code", desc=True)
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
    try:
        await manager.broadcast(message)
    except Exception as e:
        logger.warning(f"WebSocket broadcast failed: {e}")


# ==================== TRANG GIAO DIỆN ====================
@router.get("/page", response_class=HTMLResponse)
async def reception_page(request: Request, user: dict = Depends(get_current_user_or_redirect)):
    allowed = await asyncio.to_thread(can_access, "reception", user)
    if not allowed:
        return HTMLResponse(
            "⛔ Bạn không có quyền truy cập trang Lễ tân tiếp nhận!",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return templates.TemplateResponse(
        request=request,
        name="reception.html",
        context={
            "current_user": user,
            "session": request.session,
            "supabase_url": getattr(settings, "SUPABASE_URL", ""),
            "supabase_anon_key": getattr(
                settings, "SUPABASE_ANON_KEY", getattr(settings, "SUPABASE_KEY", "")
            ),
        },
    )



# ==================== 1. TIẾP NHẬN MÁY ====================
@router.post("/receive")
async def create_reception_record(
    data: ReceiveItemRequest,
    user: dict = Depends(get_current_user),
):
    phone_clean = normalize_phone(data.phone)
    if not phone_clean:
        raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ")

    now_utc = datetime.now(timezone.utc).isoformat()
    creator_name = (
        user.get("ho_ten")
        or user.get("username")
        or user.get("email")
        or ""
    ).strip()
    record = None
    final_code = None

    for attempt in range(MAX_CODE_RETRY):
        prefix, next_num = await get_next_code()
        next_num += attempt
        final_code = f"{prefix}{next_num:03d}"

        base_job_id = data.base_job_id or f"MANUAL-{final_code}-{uuid4().hex[:6]}"

        payload = {
            "code": final_code,
            "base_job_id": base_job_id,
            "customer_name": data.customer_name,
            "phone": phone_clean,
            "address": data.address,
            "note": data.note or "Chưa ghi nhận",
            "received_note": data.received_note,
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
    sms_note = data.received_note or data.note
    sms_info = generate_sms_info(phone_clean, data.customer_name, final_code, sms_note, "receive")
    return {"success": True, "message": "Đã tạo phiếu tiếp nhận!", "data": record, "sms_info": sms_info}


# ==================== 2. TRA CỨU & LỌC ====================
@router.get("/search")
async def search_reception_records(
    query_str: Optional[str] = Query(None),
    status: Optional[str] = Query("all"),
    workflow: Optional[str] = Query("all"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=200),
    user: dict = Depends(get_current_user),
):
    q = (query_str or "").strip()
    db_query = supabase.table(TABLE).select("*", count="exact")

    if workflow and workflow.strip().lower() in {"repair", "storage", "delivery"}:
        db_query = db_query.eq("current_process", workflow.strip().lower())

    if q:
        q_esc = escape_postgrest(q)
        db_query = db_query.or_(
            f"phone.ilike.%{q_esc}%,customer_name.ilike.%{q_esc}%,code.ilike.%{q_esc}%"
        )

    if status and status.strip().lower() != "all":
        st = status.strip().lower()
        if st == "received":
            db_query = db_query.eq("status", "received").eq("current_process", "repair")
        elif st in ("processing", "repairing"):
            db_query = db_query.in_("status", ["processing", "repairing"]).eq("current_process", "repair")
        elif st == "fixed":
            db_query = db_query.eq("status", "fixed").eq("current_process", "repair")
        elif st == "storage":
            db_query = (
                db_query.eq("current_process", "storage")
                .neq("status", "cancelled")
                .or_("storage_status.is.null,storage_status.neq.dispatched")
            )
        elif st == "delivery":
            db_query = (
                db_query.eq("current_process", "delivery")
                .neq("status", "cancelled")
                .or_("delivery_status.is.null,delivery_status.neq.delivered")
            )
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
        "pagination": {"page": page, "limit": limit, "total": total, "total_pages": total_pages},
    }

# ==================== 3. QUY TRÌNH KỸ THUẬT LẮP ĐẶT (KTLĐ) ====================
# LƯU Ý: Toàn bộ route /installations và các Schema phải đặt TRƯỚC route động /{record_id}
class InstallationNormalizePayload(BaseModel):
    customer_name: str
    phone: Optional[str] = ""
    address: Optional[str] = ""
    model_hint: Optional[str] = ""
    serial_number: Optional[str] = ""
    note: Optional[str] = ""


def _normalize_warranty_phone(phone: Any) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("84") and len(digits) == 11:
        digits = "0" + digits[2:]
    elif digits and not digits.startswith("0") and len(digits) in (9, 10):
        digits = "0" + digits
    return digits


def _validate_warranty_condition(cond: Any) -> str:
    val = str(cond or "").strip().lower()
    if any(k in val for k in ("qua sử dụng", "cũ", "used")):
        return "Đã qua sử dụng"
    return "Máy mới"


@router.get("/installations")
async def list_installation_orders(
    query_str: str = "",
    current_user: dict = Depends(get_current_user),
):
    q = (query_str or "").strip()

    def _run():
        req = supabase.table("installation_orders").select("*").order("created_at", desc=True).limit(200)
        if q:
            safe_q = q.replace(",", " ").replace("(", " ").replace(")", " ")
            req = req.or_(
                f"customer_name.ilike.%{safe_q}%,phone.ilike.%{safe_q}%,invoice_code.ilike.%{safe_q}%,serial_number.ilike.%{safe_q}%,model_hint.ilike.%{safe_q}%"
            )
        return req.execute()

    res = await asyncio.to_thread(_run)
    return {"success": True, "data": res.data or []}


@router.get("/installations/warranty-options")
async def get_installation_warranty_options(current_user: dict = Depends(get_current_user)):
    """Lấy danh sách chính sách bảo hành active và danh mục máy in."""
    def _fetch():
        policies_res = (
            supabase_admin.table("warranty_policy")
            .select(
                "id, policy_name, category, brand_name, condition, "
                "warranty_months, head_warranty_months, cartridge_warranty_months, "
                "page_limit_body, page_limit_head, no_page_limit, "
                "meter_limit_body, meter_limit_head, no_meter_limit, is_active"
            )
            .eq("is_active", True)
            .order("created_at", desc=True)
            .execute()
        )
        policies = policies_res.data or []

        printers = []
        for tbl in ("list_printer", "printer_models", "printers"):
            try:
                p_res = supabase_admin.table(tbl).select("brand_name, model_code, category").execute()
                if p_res.data:
                    printers = p_res.data
                    break
            except Exception:
                continue

        if not printers:
            rec_res = (
                supabase_admin.table("warranty_records")
                .select("brand_name, model_name, category")
                .limit(500)
                .execute()
            )
            seen = set()
            for r in (rec_res.data or []):
                b = str(r.get("brand_name") or "").strip().upper()
                m = re.sub(r"\s*-\s*Đầu phun mới\s*$", "", str(r.get("model_name") or "").strip(), flags=re.I)
                c = str(r.get("category") or "In phun").strip()
                key = f"{b}|{m}".lower()
                if b and m and key not in seen:
                    seen.add(key)
                    printers.append({"brand_name": b, "model_code": m, "category": c})

        return {"policies": policies, "printers": printers}

    data = await asyncio.to_thread(_fetch)
    return {"success": True, "status": "success", **data}


@router.put("/installations/{install_id}")
async def update_installation_order(
    install_id: str,
    body: InstallationNormalizePayload,
    current_user: dict = Depends(get_current_user),
):
    clean_name = (body.customer_name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Tên khách hàng không được để trống")

    clean_phone = re.sub(r"\D", "", body.phone or "")
    if clean_phone.startswith("84") and len(clean_phone) >= 11:
        clean_phone = "0" + clean_phone[2:]

    update_data = {
        "customer_name": clean_name,
        "phone": clean_phone or None,
        "address": (body.address or "").strip() or None,
        "model_hint": (body.model_hint or "").strip().upper() or None,
        "serial_number": (body.serial_number or "").strip().upper() or None,
        "note": (body.note or "").strip() or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    res = await asyncio.to_thread(
        lambda: supabase.table("installation_orders").update(update_data).eq("id", install_id).execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn lắp đặt")
    return {"success": True, "data": res.data[0]}


@router.post("/installations/{order_id}/create-warranties")
async def create_installation_warranties(
    order_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Tạo 1 hoặc nhiều phiếu bảo hành vào bảng warranty_records từ đơn Kỹ Thuật Lắp Đặt."""
    body = await request.json()
    customer_name = str(body.get("customer_name") or "").strip().title()
    phone_number = _normalize_warranty_phone(body.get("phone_number"))
    purchase_date = str(body.get("purchase_date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    items = body.get("items") or []

    if not customer_name:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Vui lòng nhập tên khách hàng!"})
    if not phone_number or not phone_number.isdigit() or not (9 <= len(phone_number) <= 11):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Số điện thoại phải gồm 9 - 11 chữ số!"})
    if not items:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Chưa có thiết bị nào để tạo phiếu bảo hành!"})

    input_sns = [str(it.get("serial_number") or "").strip().upper() for it in items]
    if any(not sn for sn in input_sns):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Số Serial (S/N) không được để trống!"})
    if len(input_sns) != len(set(input_sns)):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Có số Serial bị trùng lặp trong cùng đơn!"})

    staff_display = (
        user.get("ho_ten")
        or user.get("display_name")
        or user.get("full_name")
        or user.get("username")
        or "Kỹ thuật"
    )

    def _execute():
        order_res = (
            supabase_admin.table("installation_orders")
            .select("*")
            .eq("id", order_id)
            .limit(1)
            .execute()
        )
        if not order_res.data:
            return {"error": (404, "Không tìm thấy đơn lắp đặt!")}
        order = order_res.data[0]

        exist_sn_res = (
            supabase_admin.table("warranty_records")
            .select("serial_number")
            .in_("serial_number", input_sns)
            .execute()
        )
        if exist_sn_res.data:
            dup_list = [r["serial_number"] for r in exist_sn_res.data]
            return {"error": (400, f"Số Serial đã tồn tại trên hệ thống bảo hành: {', '.join(dup_list)}")}

        pol_res = (
            supabase_admin.table("warranty_policy")
            .select("*")
            .eq("is_active", True)
            .execute()
        )
        policy_map = {str(p["id"]): p for p in (pol_res.data or []) if p.get("id")}

        now_dt = datetime.now()
        now_str = now_dt.strftime("%H:%M %d/%m/%Y")
        invoice_ref = f" | ĐH: {order.get('invoice_code')}" if order.get("invoice_code") else ""

        records_to_insert = []
        for idx, it in enumerate(items, start=1):
            sn = str(it.get("serial_number") or "").strip().upper()
            brand = str(it.get("brand_name") or "").strip().upper()
            model = str(it.get("model_name") or "").strip()
            category = str(it.get("category") or "In phun").strip()
            condition = _validate_warranty_condition(it.get("condition"))
            ink_type = str(it.get("ink_type") or "").strip()
            policy_id = str(it.get("policy_id") or "").strip()

            if not brand or not model:
                return {"error": (400, f"Máy #{idx} ({sn}): Vui lòng nhập đầy đủ Hãng và Model!")}
            if not ink_type:
                return {"error": (400, f"Máy #{idx} ({sn}): Vui lòng chọn Loại mực!")}
            if not policy_id or policy_id not in policy_map:
                return {"error": (400, f"Máy #{idx} ({sn}): Vui lòng chọn Gói bảo hành hợp lệ!")}

            if condition == "Đã qua sử dụng" and it.get("new_head") and "đầu phun mới" not in model.lower():
                model = f"{model} - Đầu phun mới"
            elif condition == "Máy mới":
                model = re.sub(r"\s*-\s*Đầu phun mới\s*$", "", model, flags=re.I).strip()

            try:
                init_counter = max(0, int(it.get("initial_counter") or 0))
            except (ValueError, TypeError):
                init_counter = 0

            p_info = policy_map[policy_id]
            p_name = p_info.get("policy_name", "N/A")

            w_months = int(p_info.get("warranty_months") or 0)
            h_months = int(p_info.get("head_warranty_months") or 0) if category != "In Laser" else 0
            c_months = int(p_info.get("cartridge_warranty_months") or 0) if category == "In Laser" else 0
            p_body = int(p_info.get("page_limit_body") or 0)
            p_head = int(p_info.get("page_limit_head") or 0)
            is_no_limit = bool(p_info.get("no_page_limit", False))

            log_entry = (
                f"• [{now_str}] {staff_display}: KHỞI TẠO (Gói: {p_name}) | "
                f"Máy: {brand} {model} ({ink_type}) | S/N: {sn}{invoice_ref}"
            )

            records_to_insert.append({
                "policy_id": policy_id,
                "customer_name": customer_name,
                "phone_number": phone_number,
                "purchase_date": purchase_date,
                "category": category,
                "condition": condition,
                "brand_name": brand,
                "model_name": model,
                "ink_type": ink_type,
                "serial_number": sn,
                "warranty_months": w_months,
                "head_warranty_months": h_months,
                "cartridge_warranty_months": c_months,
                "initial_counter": init_counter,
                "current_counter": init_counter,
                "page_limit_body": p_body,
                "page_limit_head": p_head,
                "no_page_limit": is_no_limit,
                "staff_name": staff_display,
                "edit_log": log_entry,
                "pages_updated_at": now_dt.isoformat(),
            })

        ins_res = supabase_admin.table("warranty_records").insert(records_to_insert).execute()
        created_rows = ins_res.data or []
        if not created_rows:
            return {"error": (500, "Không thể ghi phiếu bảo hành vào Supabase!")}

        first_id = created_rows[0].get("id")
        all_sns_str = ", ".join(r["serial_number"] for r in created_rows)

        supabase_admin.table("installation_orders").update({
            "customer_name": customer_name,
            "phone": phone_number,
            "serial_number": all_sns_str,
            "warranty_record_id": first_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", order_id).execute()

        return {"created": created_rows}

    result = await asyncio.to_thread(_execute)
    if "error" in result:
        code, msg = result["error"]
        return JSONResponse(status_code=code, content={"status": "error", "message": msg})

    return {
        "success": True,
        "status": "success",
        "message": f"Đã tạo thành công {len(result['created'])} phiếu bảo hành!",
        "records": result["created"],
    }


# ==================== 4. CHI TIẾT PHIẾU (ROUTE ĐỘNG ĐẶT SAU CÙNG) ====================
@router.get("/{record_id}")
async def get_reception_record(record_id: str, user: dict = Depends(get_current_user)):
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


# ==================== 5. CẬP NHẬT PHIẾU ====================
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
        phone_clean = normalize_phone(data.phone)
        if not phone_clean:
            raise HTTPException(status_code=400, detail="Số điện thoại không hợp lệ")
        update_payload["phone"] = phone_clean
    if data.address is not None:
        update_payload["address"] = data.address
    if data.note is not None:
        update_payload["note"] = data.note
    if data.received_note is not None:
        update_payload["received_note"] = data.received_note
    if data.status is not None:
        st_val = str(data.status).strip().lower()
        if st_val == "repairing":
            st_val = "processing"

        if st_val == "returned":
            raise HTTPException(
                status_code=400,
                detail="Không được phép chuyển trạng thái 'returned' qua API Cập nhật. Vui lòng sử dụng API Trả Máy riêng.",
            )

        if st_val not in ALLOWED_STATUSES:
            raise HTTPException(status_code=400, detail=f"Trạng thái không hợp lệ: {st_val}")
        update_payload["status"] = st_val

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
    return {"success": True, "message": "Đã cập nhật thông tin phiếu!", "data": updated_record}


# ==================== 6. TRẢ MÁY ====================
@router.post("/{record_id}/return")
@router.post("/return/{record_id}")
async def return_item_to_customer(
    record_id: str,
    data: ReturnItemRequest = Body(default_factory=ReturnItemRequest),
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
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")

    record = existing.data[0]
    current_status = str(record.get("status") or "").lower()
    current_process = str(record.get("current_process") or "repair").lower()
    storage_status = str(record.get("storage_status") or "").lower()
    delivery_status = str(record.get("delivery_status") or "").lower()

    if storage_status == "destroyed":
        raise HTTPException(status_code=400, detail="Thiết bị đã được xác nhận tiêu hủy tại kho, không thể trả khách.")
    if delivery_status == "failed":
        raise HTTPException(status_code=400, detail="Đơn giao nhận đang ở trạng thái thất bại.")
    if current_process == "delivery":
        raise HTTPException(status_code=400, detail="Đơn đang thuộc Quy trình Giao nhận! Bàn giao do Shipper thực hiện.")
    if current_process == "repair" and current_status in {"processing", "repairing"}:
        raise HTTPException(status_code=400, detail="Thiết bị đang sửa chữa! Cần hoàn thành trước khi trả máy.")
    if current_status == "returned" or storage_status == "dispatched":
        raise HTTPException(status_code=400, detail="Phiếu này đã được xác nhận trả trước đó.")

    now_utc = datetime.now(timezone.utc).isoformat()
    payload = {"status": "returned", "returned_date": now_utc, "updated_at": now_utc}

    if current_process == "storage":
        payload["storage_status"] = "dispatched"
    if data.returned_note:
        payload["returned_note"] = data.returned_note
    if data.returned_method:
        payload["returned_method"] = data.returned_method
    if data.received_note:
        payload["received_note"] = data.received_note
    if data.is_fixed is not None:
        payload["is_fixed"] = data.is_fixed
        payload["repair_result"] = "fixed" if data.is_fixed else "failed"

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE).update(payload).eq("id", record_id).execute()
        )
    except Exception as e:
        logger.error(f"Lỗi trả máy: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Không thể cập nhật trạng thái trả máy")
    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")

    updated = res.data[0]
    await safe_broadcast({"type": "reception_returned", "record": updated})
    sms_info = generate_sms_info(
        updated.get("phone"),
        updated.get("customer_name"),
        updated.get("code"),
        data.returned_note,
        "return",
    )
    return {"success": True, "message": "Đã ghi nhận trả máy!", "data": updated, "sms_info": sms_info}


# ==================== 7. XÓA VĨNH VIỄN PHIẾU (Hard Delete) ====================
@router.delete("/{ticket_id}")
async def delete_reception_ticket(ticket_id: str, user: dict = Depends(get_current_user)):
    try:
        UUID(ticket_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    allowed = await asyncio.to_thread(can_access, "reception", user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền xóa phiếu theo phân quyền hệ thống.",
        )

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE).delete().eq("id", ticket_id).execute()
        )
    except Exception as e:
        logger.error(f"Lỗi khi xóa phiếu: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xóa phiếu")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu cần xóa")

    deleted_record = res.data[0]
    await safe_broadcast({"type": "reception_deleted", "record": deleted_record})
    return {
        "success": True,
        "status": "success",
        "message": f"Đã xóa vĩnh viễn phiếu {deleted_record.get('code', ticket_id)}",
        "data": deleted_record,
    }


# ==================== 8. XỬ LÝ VẤN ĐỀ SMS ĐÃ GỬI - LINK ĐÃ GỬI ====================
@router.post("/{record_id}/sms-sent")
async def mark_reception_sms_sent(record_id: str, user: dict = Depends(get_current_user)):
    try:
        UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    now_utc = datetime.now(timezone.utc).isoformat()
    sender_name = (
        user.get("ho_ten")
        or user.get("username")
        or user.get("email")
        or ""
    ).strip()

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE)
            .update({
                "sms_sent_at": now_utc,
                "sms_sent_by": sender_name,
            })
            .eq("id", record_id)
            .execute()
        )
    except Exception as e:
        logger.error(f"Lỗi lưu trạng thái đã gửi SMS: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Không thể lưu trạng thái gửi SMS")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")

    updated_record = res.data[0]
    await safe_broadcast({"type": "reception_updated", "record": updated_record})
    return {"success": True, "data": updated_record}


@router.post("/{record_id}/link-sent")
async def mark_reception_link_sent(record_id: str, user: dict = Depends(get_current_user)):
    try:
        UUID(record_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID phiếu không hợp lệ")

    now_utc = datetime.now(timezone.utc).isoformat()
    sender_name = (
        user.get("ho_ten")
        or user.get("username")
        or user.get("email")
        or ""
    ).strip()

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table(TABLE)
            .update({
                "link_sent_at": now_utc,
                "link_sent_by": sender_name,
            })
            .eq("id", record_id)
            .execute()
        )
    except Exception as e:
        logger.error(f"Lỗi lưu trạng thái đã gửi link: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Không thể lưu trạng thái gửi link")

    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu")

    updated_record = res.data[0]
    await safe_broadcast({"type": "reception_updated", "record": updated_record})
    return {"success": True, "data": updated_record}
