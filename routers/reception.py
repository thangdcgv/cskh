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

def _get_actor_name(user: Optional[dict]) -> str:
    if not user or not isinstance(user, dict):
        return ""
    return str(
        user.get("ho_ten")
        or user.get("display_name")
        or user.get("full_name")
        or user.get("username")
        or user.get("email")
        or ""
    ).strip()


def _safe_table_update(client, table_name: str, payload: dict, match_col: str, match_val: Any):
    """Cập nhật bảng và tự bỏ qua cột audit nếu DB chưa chạy ALTER TABLE."""
    data = dict(payload)
    for _ in range(8):
        try:
            return client.table(table_name).update(data).eq(match_col, match_val).execute()
        except Exception as exc:
            msg = str(exc)
            m = re.search(r"['\"]([a-zA-Z0-9_]+)['\"] column", msg) or re.search(
                r'column "([a-zA-Z0-9_]+)"', msg
            )
            if m and m.group(1) in data:
                bad_col = m.group(1)
                logger.warning(f"Bỏ qua cột chưa tồn tại {table_name}.{bad_col}")
                data.pop(bad_col, None)
                continue
            raise
    return client.table(table_name).update(data).eq(match_col, match_val).execute()


def _safe_table_insert(client, table_name: str, payload: dict):
    """Insert bảng và tự bỏ qua cột audit nếu DB chưa chạy ALTER TABLE."""
    data = dict(payload)
    for _ in range(8):
        try:
            return client.table(table_name).insert(data).execute()
        except Exception as exc:
            msg = str(exc)
            m = re.search(r"['\"]([a-zA-Z0-9_]+)['\"] column", msg) or re.search(
                r'column "([a-zA-Z0-9_]+)"', msg
            )
            if m and m.group(1) in data:
                bad_col = m.group(1)
                logger.warning(f"Bỏ qua cột chưa tồn tại {table_name}.{bad_col}")
                data.pop(bad_col, None)
                continue
            raise
    return client.table(table_name).insert(data).execute()

# ==================== TRANG GIAO DIỆN ====================
@router.get("/page", response_class=HTMLResponse)
async def reception_page(request: Request, user: dict = Depends(get_current_user_or_redirect)):
    allowed = await asyncio.to_thread(can_access, "reception", user)
    if not allowed:
        return HTMLResponse(
            "⛔ Bạn không có quyền truy cập trang Lễ tân tiếp nhận!",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    def _resolve_page_perms() -> dict:
        can_delete = bool(can_access("reception_delete", user))
        can_w_create = bool(check_ktld_warranty_permission("installation_warranty_create", user))
        can_w_edit = bool(check_ktld_warranty_permission("installation_warranty_edit", user))
        can_w_delete = bool(check_ktld_warranty_permission("installation_warranty_delete", user))
        return {
            "can_delete": can_delete,
            # Giữ cờ cũ để tương thích ngược với giao diện reception.html hiện tại
            "can_install_warranty": bool(can_w_create or can_w_edit or can_w_delete),
            # 3 quyền chi tiết lấy từ system_settings
            "can_install_warranty_create": can_w_create,
            "can_install_warranty_edit": can_w_edit,
            "can_install_warranty_delete": can_w_delete,
        }

    user_perms = await asyncio.to_thread(_resolve_page_perms)

    return templates.TemplateResponse(
        request=request,
        name="reception.html",
        context={
            "current_user": user,
            "user_perms": user_perms,
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
            "created_by": creator_name or None,
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
    staff_name: Optional[str] = Query(None),
    staff_role: Optional[str] = Query("all"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
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
    if date_from and re.match(r"^\d{4}-\d{2}-\d{2}$", date_from.strip()):
        db_query = db_query.gte("created_at", f"{date_from.strip()}T00:00:00+07:00")
    if date_to and re.match(r"^\d{4}-\d{2}-\d{2}$", date_to.strip()):
        db_query = db_query.lte("created_at", f"{date_to.strip()}T23:59:59+07:00")

    s_name = (staff_name or "").strip()
    s_role = (staff_role or "all").strip().lower()
    if s_name:
        s_esc = escape_postgrest(s_name)
        role_cols = {
            "creator": ["created_by"],
            "updater": ["updated_by"],
            "responder": ["returned_by", "sms_sent_by", "link_sent_by"],
            "all": ["created_by", "updated_by", "returned_by", "sms_sent_by", "link_sent_by"],
        }.get(s_role, ["created_by", "updated_by", "returned_by", "sms_sent_by", "link_sent_by"])
        or_expr = ",".join(f"{col}.ilike.%{s_esc}%" for col in role_cols)
        db_query = db_query.or_(or_expr)
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

_ktld_warranty_perm_cache = {"data": {}, "expires_at": 0.0}
KTLD_WARRANTY_PERM_KEYS = (
    "installation_warranty_create",
    "installation_warranty_edit",
    "installation_warranty_delete",
)


def _get_ktld_warranty_settings() -> Dict[str, List[str]]:
    now = time.time()
    if now < _ktld_warranty_perm_cache["expires_at"]:
        return _ktld_warranty_perm_cache["data"]
    try:
        res = (
            supabase_admin.table("system_settings")
            .select("key, value")
            .in_("key", list(KTLD_WARRANTY_PERM_KEYS))
            .execute()
        )
        parsed: Dict[str, List[str]] = {}
        for item in (res.data or []):
            raw = str(item.get("value") or "").strip()
            if raw.startswith("["):
                try:
                    import json
                    parsed[item["key"]] = [str(x).strip().lower() for x in json.loads(raw) if str(x).strip()]
                    continue
                except Exception:
                    raw = raw.strip("[]")
            parsed[item["key"]] = [x.strip().strip("\"'").lower() for x in raw.split(",") if x.strip()]
    except Exception as e:
        logger.warning(f"Không đọc được phân quyền bảo hành KTLĐ từ system_settings: {e}")
        parsed = {}

    _ktld_warranty_perm_cache["data"] = parsed
    _ktld_warranty_perm_cache["expires_at"] = now + 60
    return parsed


def check_ktld_warranty_permission(setting_key: str, user: dict) -> bool:
    """Kiểm tra quyền thêm/sửa/xóa phiếu BH KTLĐ từ system_settings (fallback sang can_access)."""
    if not user or not isinstance(user, dict):
        return False

    user_tags = {
        str(user.get(field) or "").strip().lower()
        for field in ("role", "user_role", "department", "phong_ban", "email", "id")
        if user.get(field)
    }
    if user.get("is_admin") or "admin" in user_tags or "super admin" in user_tags:
        return True

    cfg = _get_ktld_warranty_settings()
    if setting_key in cfg:
        allowed_values = set(cfg[setting_key])
        return "*" in allowed_values or bool(user_tags & allowed_values)

    # Fallback về key cũ trong page_permissions nếu system_settings chưa có bản ghi
    if setting_key == "installation_warranty_delete":
        return can_access("reception_delete", user)
    return can_access("installation_warranty", user)

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


def _fetch_order_warranty_records(order: dict) -> List[dict]:
    """Lấy tất cả phiếu bảo hành thuộc 1 đơn KTLĐ (hỗ trợ đơn nhiều máy)."""
    found_map: Dict[str, dict] = {}
    first_id = str(order.get("warranty_record_id") or "").strip()
    serials = [
        s.strip().upper()
        for s in str(order.get("serial_number") or "").split(",")
        if s.strip()
    ]

    if first_id:
        res_id = (
            supabase_admin.table("warranty_records")
            .select("*")
            .eq("id", first_id)
            .limit(1)
            .execute()
        )
        for row in (res_id.data or []):
            found_map[str(row["id"])] = row

    if serials:
        res_sn = (
            supabase_admin.table("warranty_records")
            .select("*")
            .in_("serial_number", serials)
            .execute()
        )
        for row in (res_sn.data or []):
            found_map[str(row["id"])] = row

    rows = list(found_map.values())
    rows.sort(key=lambda r: str(r.get("created_at") or ""))
    return rows


def _extract_latest_editor_from_log(edit_log: str) -> str:
    """Trích tên người sửa gần nhất từ dòng log 'SỬA PHIẾU BH' hoặc 'CẬP NHẬT'."""
    if not edit_log:
        return ""
    for line in reversed(str(edit_log).splitlines()):
        m = re.search(
            r"\]\s*([^:]+):\s*(?:SỬA PHIẾU BH|CẬP NHẬT|CHỈNH SỬA)",
            line,
            flags=re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
    return ""


@router.get("/installations")
async def list_installation_orders(
    query_str: str = "",
    staff_name: str = "",
    staff_role: str = "all",
    date_from: str = "",
    date_to: str = "",
    current_user: dict = Depends(get_current_user),
):
    q = (query_str or "").strip()

    def _run():
        req = supabase.table("installation_orders").select("*").order("created_at", desc=True).limit(200)
        if date_from and re.match(r"^\d{4}-\d{2}-\d{2}$", date_from.strip()):
            req = req.gte("created_at", f"{date_from.strip()}T00:00:00+07:00")
        if date_to and re.match(r"^\d{4}-\d{2}-\d{2}$", date_to.strip()):
            req = req.lte("created_at", f"{date_to.strip()}T23:59:59+07:00")
        if q:
            safe_q = q.replace(",", " ").replace("(", " ").replace(")", " ")
            req = req.or_(
                f"customer_name.ilike.%{safe_q}%,phone.ilike.%{safe_q}%,invoice_code.ilike.%{safe_q}%,"
                f"serial_number.ilike.%{safe_q}%,model_hint.ilike.%{safe_q}%,assignee_name.ilike.%{safe_q}%"
            )
        res = req.execute()
        orders = res.data or []

        # Làm giàu thông tin người tạo / người sửa phiếu BH từ warranty_records
        w_ids = [str(o["warranty_record_id"]) for o in orders if o.get("warranty_record_id")]
        all_sns = []
        for o in orders:
            if o.get("warranty_record_id") and o.get("serial_number"):
                for sn in str(o["serial_number"]).split(","):
                    clean_sn = sn.strip().upper()
                    if clean_sn:
                        all_sns.append(clean_sn)

        w_by_id: Dict[str, dict] = {}
        w_by_sn: Dict[str, dict] = {}
        if w_ids:
            w_res = (
                supabase_admin.table("warranty_records")
                .select("id, serial_number, staff_name, edit_log, updated_by, created_at")
                .in_("id", list(set(w_ids)))
                .execute()
            )
            for wr in (w_res.data or []):
                w_by_id[str(wr["id"])] = wr
                if wr.get("serial_number"):
                    w_by_sn[str(wr["serial_number"]).upper()] = wr

        if all_sns:
            sn_res = (
                supabase_admin.table("warranty_records")
                .select("id, serial_number, staff_name, edit_log, updated_by, created_at")
                .in_("serial_number", list(set(all_sns)))
                .execute()
            )
            for wr in (sn_res.data or []):
                w_by_id[str(wr["id"])] = wr
                if wr.get("serial_number"):
                    w_by_sn[str(wr["serial_number"]).upper()] = wr

        for o in orders:
            linked_w = []
            wid = str(o.get("warranty_record_id") or "")
            if wid and wid in w_by_id:
                linked_w.append(w_by_id[wid])
            for sn in str(o.get("serial_number") or "").split(","):
                clean_sn = sn.strip().upper()
                if clean_sn and clean_sn in w_by_sn:
                    wr = w_by_sn[clean_sn]
                    if all(str(x["id"]) != str(wr["id"]) for x in linked_w):
                        linked_w.append(wr)

            if linked_w:
                first_w = linked_w[0]
                if not o.get("warranty_created_by"):
                    o["warranty_created_by"] = first_w.get("staff_name") or ""
                if not o.get("warranty_updated_by"):
                    latest_ed = ""
                    for wr in linked_w:
                        latest_ed = wr.get("updated_by") or _extract_latest_editor_from_log(wr.get("edit_log") or "") or latest_ed
                    o["warranty_updated_by"] = latest_ed
                logs = [str(wr.get("edit_log") or "").strip() for wr in linked_w if wr.get("edit_log")]
                o["warranty_edit_log"] = "\n".join(logs)

        s_kw = (staff_name or "").strip().lower()
        s_role = (staff_role or "all").strip().lower()
        if s_kw or s_role != "all":
            filtered_orders = []
            for o in orders:
                pools = {
                    "creator": [o.get("warranty_created_by")],
                    "updater": [o.get("warranty_updated_by"), o.get("updated_by")],
                    "assignee": [o.get("assignee_name")],
                    "all": [
                        o.get("warranty_created_by"),
                        o.get("warranty_updated_by"),
                        o.get("updated_by"),
                        o.get("assignee_name"),
                    ],
                }
                vals = [str(v).strip() for v in (pools.get(s_role) or pools["all"]) if v]
                if s_role != "all" and not s_kw and not vals:
                    continue
                if s_kw and not any(s_kw in v.lower() for v in vals):
                    continue
                filtered_orders.append(o)
            return filtered_orders

        return orders

    orders_data = await asyncio.to_thread(_run)
    return {"success": True, "data": orders_data}


@router.get("/installations/warranty-options")
async def get_installation_warranty_options(current_user: dict = Depends(get_current_user)):
    """Lấy danh sách chính sách bảo hành active và danh mục máy in."""
    def _can_view_options() -> bool:
        return (
            check_ktld_warranty_permission("installation_warranty_create", current_user)
            or check_ktld_warranty_permission("installation_warranty_edit", current_user)
        )

    if not await asyncio.to_thread(_can_view_options):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="⛔ Bạn hoặc phòng ban của bạn không có quyền tạo/sửa phiếu bảo hành từ đơn KTLĐ!",
        )
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

    now_utc = datetime.now(timezone.utc).isoformat()
    actor_name = _get_actor_name(current_user)

    update_data = {
        "customer_name": clean_name,
        "phone": clean_phone or None,
        "address": (body.address or "").strip() or None,
        "model_hint": (body.model_hint or "").strip().upper() or None,
        "serial_number": (body.serial_number or "").strip().upper() or None,
        "note": (body.note or "").strip() or None,
        "updated_by": actor_name or None,
        "updated_by_at": now_utc,
        "updated_at": now_utc,
    }

    res = await asyncio.to_thread(
        lambda: _safe_table_update(supabase, "installation_orders", update_data, "id", install_id)
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn lắp đặt")
    return {"success": True, "data": res.data[0]}


@router.get("/installations/{order_id}/warranties")
async def get_installation_warranties(
    order_id: str,
    user: dict = Depends(get_current_user),
):
    """Lấy toàn bộ phiếu bảo hành đã tạo từ đơn KTLĐ để xem/chỉnh sửa."""
    def _can_view_warranties() -> bool:
        return (
            check_ktld_warranty_permission("installation_warranty_create", user)
            or check_ktld_warranty_permission("installation_warranty_edit", user)
            or check_ktld_warranty_permission("installation_warranty_delete", user)
        )

    if not await asyncio.to_thread(_can_view_warranties):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="⛔ Bạn hoặc phòng ban của bạn không có quyền xem/sửa phiếu bảo hành từ đơn KTLĐ!",
        )
    """Lấy toàn bộ phiếu bảo hành đã tạo từ đơn KTLĐ để xem/chỉnh sửa."""
    def _fetch():
        order_res = (
            supabase_admin.table("installation_orders")
            .select("*")
            .eq("id", order_id)
            .limit(1)
            .execute()
        )
        if not order_res.data:
            return None, []
        order = order_res.data[0]
        records = _fetch_order_warranty_records(order)
        return order, records

    order, records = await asyncio.to_thread(_fetch)
    if not order:
        raise HTTPException(status_code=404, detail="Không tìm thấy đơn lắp đặt")

    return {"success": True, "order": order, "records": records}


@router.post("/installations/{order_id}/create-warranties")
async def create_installation_warranties(
    order_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Tạo 1 hoặc nhiều phiếu bảo hành vào bảng warranty_records từ đơn Kỹ Thuật Lắp Đặt."""
    allowed = await asyncio.to_thread(
        check_ktld_warranty_permission, "installation_warranty_create", user
    )
    if not allowed:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "success": False,
                "status": "error",
                "message": "⛔ Bạn hoặc phòng ban của bạn không có quyền cấp phiếu bảo hành từ đơn KTLĐ!",
            },
        )

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

    staff_display = _get_actor_name(user) or "Kỹ thuật"

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
        now_utc_iso = datetime.now(timezone.utc).isoformat()
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

        _safe_table_update(
            supabase_admin,
            "installation_orders",
            {
                "customer_name": customer_name,
                "phone": phone_number,
                "serial_number": all_sns_str,
                "warranty_record_id": first_id,
                "warranty_created_by": staff_display,
                "warranty_created_at": now_utc_iso,
                "updated_at": now_utc_iso,
            },
            "id",
            order_id,
        )

        return {"created": created_rows, "created_by": staff_display}

    result = await asyncio.to_thread(_execute)
    if "error" in result:
        code, msg = result["error"]
        return JSONResponse(status_code=code, content={"status": "error", "message": msg})

    return {
        "success": True,
        "status": "success",
        "message": f"Đã tạo thành công {len(result['created'])} phiếu bảo hành!",
        "records": result["created"],
        "warranty_created_by": result["created_by"],
    }


@router.put("/installations/{order_id}/warranties")
async def update_installation_warranties(
    order_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Chỉnh sửa lại phiếu bảo hành đã tạo từ đơn KTLĐ (hỗ trợ sửa nhiều máy & bổ sung máy)."""
    allowed = await asyncio.to_thread(
        check_ktld_warranty_permission, "installation_warranty_edit", user
    )
    if not allowed:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "success": False,
                "status": "error",
                "message": "⛔ Bạn hoặc phòng ban của bạn không có quyền chỉnh sửa phiếu bảo hành từ đơn KTLĐ!",
            },
        )

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
        return JSONResponse(status_code=400, content={"status": "error", "message": "Phải có ít nhất 1 thiết bị bảo hành!"})

    input_sns = [str(it.get("serial_number") or "").strip().upper() for it in items]
    if any(not sn for sn in input_sns):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Số Serial (S/N) không được để trống!"})
    if len(input_sns) != len(set(input_sns)):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Có số Serial bị trùng lặp trong cùng đơn!"})

    editor_display = _get_actor_name(user) or "Kỹ thuật"

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

        existing_records = _fetch_order_warranty_records(order)
        existing_by_id = {str(r["id"]): r for r in existing_records}
        owned_ids = set(existing_by_id.keys())

        exist_sn_res = (
            supabase_admin.table("warranty_records")
            .select("id, serial_number")
            .in_("serial_number", input_sns)
            .execute()
        )
        conflict_sns = [
            r["serial_number"]
            for r in (exist_sn_res.data or [])
            if str(r.get("id")) not in owned_ids
        ]
        if conflict_sns:
            return {
                "error": (
                    400,
                    f"Số Serial đã tồn tại ở phiếu bảo hành khác: {', '.join(conflict_sns)}",
                )
            }

        pol_res = (
            supabase_admin.table("warranty_policy")
            .select("*")
            .execute()
        )
        policy_map = {str(p["id"]): p for p in (pol_res.data or []) if p.get("id")}

        now_dt = datetime.now()
        now_utc_iso = datetime.now(timezone.utc).isoformat()
        now_str = now_dt.strftime("%H:%M %d/%m/%Y")
        invoice_ref = f" | ĐH: {order.get('invoice_code')}" if order.get("invoice_code") else ""

        final_rows = []
        for idx, it in enumerate(items, start=1):
            rec_id = str(it.get("id") or "").strip()
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

            if rec_id and rec_id in existing_by_id:
                old_rec = existing_by_id[rec_id]
                diffs = []
                if (old_rec.get("customer_name") or "") != customer_name:
                    diffs.append(f"KH: {old_rec.get('customer_name')} → {customer_name}")
                if (old_rec.get("phone_number") or "") != phone_number:
                    diffs.append(f"SĐT: {old_rec.get('phone_number')} → {phone_number}")
                if str(old_rec.get("purchase_date") or "")[:10] != purchase_date:
                    diffs.append(f"Ngày BH: {str(old_rec.get('purchase_date') or '')[:10]} → {purchase_date}")
                if (old_rec.get("serial_number") or "").upper() != sn:
                    diffs.append(f"S/N: {old_rec.get('serial_number')} → {sn}")
                if (old_rec.get("brand_name") or "").upper() != brand or (old_rec.get("model_name") or "") != model:
                    diffs.append(f"Máy: {old_rec.get('brand_name')} {old_rec.get('model_name')} → {brand} {model}")
                if (old_rec.get("ink_type") or "") != ink_type:
                    diffs.append(f"Mực: {old_rec.get('ink_type')} → {ink_type}")
                if str(old_rec.get("policy_id") or "") != policy_id:
                    diffs.append(f"Gói BH: {p_name}")

                diff_text = "; ".join(diffs) if diffs else "Cập nhật thông tin phiếu"
                old_log = str(old_rec.get("edit_log") or "").strip()
                new_log_line = f"• [{now_str}] {editor_display}: SỬA PHIẾU BH ({diff_text})"
                combined_log = f"{old_log}\n{new_log_line}".strip()

                old_init = int(old_rec.get("initial_counter") or 0)
                old_curr = int(old_rec.get("current_counter") or 0)
                next_curr = init_counter if old_curr == old_init else max(old_curr, init_counter)

                upd_payload = {
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
                    "current_counter": next_curr,
                    "page_limit_body": p_body,
                    "page_limit_head": p_head,
                    "no_page_limit": is_no_limit,
                    "updated_by": editor_display,
                    "edit_log": combined_log,
                }
                upd_res = _safe_table_update(supabase_admin, "warranty_records", upd_payload, "id", rec_id)
                if upd_res.data:
                    final_rows.append(upd_res.data[0])
            else:
                log_entry = (
                    f"• [{now_str}] {editor_display}: BỔ SUNG MÁY BH (Gói: {p_name}) | "
                    f"Máy: {brand} {model} ({ink_type}) | S/N: {sn}{invoice_ref}"
                )
                ins_payload = {
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
                    "staff_name": editor_display,
                    "updated_by": editor_display,
                    "edit_log": log_entry,
                    "pages_updated_at": now_dt.isoformat(),
                }
                ins_res = _safe_table_insert(supabase_admin, "warranty_records", ins_payload)
                if ins_res.data:
                    final_rows.append(ins_res.data[0])

        first_id = final_rows[0].get("id") if final_rows else order.get("warranty_record_id")
        all_sns_str = ", ".join(r["serial_number"] for r in final_rows)
        all_logs = "\n".join(str(r.get("edit_log") or "").strip() for r in final_rows if r.get("edit_log"))

        _safe_table_update(
            supabase_admin,
            "installation_orders",
            {
                "customer_name": customer_name,
                "phone": phone_number,
                "serial_number": all_sns_str,
                "warranty_record_id": first_id,
                "warranty_updated_by": editor_display,
                "warranty_updated_at": now_utc_iso,
                "updated_at": now_utc_iso,
            },
            "id",
            order_id,
        )

        return {
            "records": final_rows,
            "updated_by": editor_display,
            "edit_log": all_logs,
        }

    result = await asyncio.to_thread(_execute)
    if "error" in result:
        code, msg = result["error"]
        return JSONResponse(status_code=code, content={"status": "error", "message": msg})

    return {
        "success": True,
        "status": "success",
        "message": f"Đã cập nhật thành công {len(result['records'])} phiếu bảo hành!",
        "records": result["records"],
        "warranty_updated_by": result["updated_by"],
        "warranty_edit_log": result["edit_log"],
    }

@router.delete("/installations/{order_id}/warranties/{warranty_id}")
async def delete_installation_warranty_record(
    order_id: str,
    warranty_id: str,
    user: dict = Depends(get_current_user),
):
    allowed = await asyncio.to_thread(
        check_ktld_warranty_permission, "installation_warranty_delete", user
    )
    if not allowed:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"status": "error", "message": "⛔ Bạn không có quyền xóa phiếu bảo hành KTLĐ!"},
        )

    def _delete_warranty():
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

        records = _fetch_order_warranty_records(order)
        target = next((r for r in records if str(r.get("id")) == str(warranty_id)), None)
        if not target:
            return {"error": (404, "Phiếu bảo hành không thuộc đơn lắp đặt này!")}

        supabase_admin.table("warranty_records").delete().eq("id", warranty_id).execute()

        remaining = [r for r in records if str(r.get("id")) != str(warranty_id)]
        now_utc_iso = datetime.now(timezone.utc).isoformat()
        order_patch = {
            "warranty_record_id": remaining[0]["id"] if remaining else None,
            "serial_number": ", ".join(r["serial_number"] for r in remaining) if remaining else None,
            "updated_by": _get_actor_name(user) or None,
            "updated_at": now_utc_iso,
        }
        if not remaining:
            order_patch["warranty_created_by"] = None
            order_patch["warranty_created_at"] = None

        _safe_table_update(
            supabase_admin,
            "installation_orders",
            order_patch,
            "id",
            order_id,
        )
        return {"deleted_sn": target.get("serial_number"), "remaining": remaining}

    result = await asyncio.to_thread(_delete_warranty)
    if "error" in result:
        code, msg = result["error"]
        return JSONResponse(status_code=code, content={"status": "error", "message": msg})

    return {
        "success": True,
        "status": "success",
        "message": f"Đã xóa phiếu bảo hành S/N: {result['deleted_sn']}",
        "records": result["remaining"],
    }

@router.delete("/installations/{order_id}")
async def delete_installation_order(order_id: str, user: dict = Depends(get_current_user)):
    # 1. Kiểm tra quyền xóa đồng bộ với delete_reception_ticket
    allowed = await asyncio.to_thread(can_access, "reception_delete", user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền xóa đơn lắp đặt theo phân quyền hệ thống!",
        )

    def _delete_tx():
        res = (
            supabase_admin.table("installation_orders")
            .select("id, status, warranty_record_id, invoice_code, base_job_id, customer_name, serial_number")
            .eq("id", order_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return {"error": (404, "Không tìm thấy đơn kỹ thuật lắp đặt!")}

        order = res.data[0]
        st = str(order.get("status") or "pending").strip().lower()
        code_label = order.get("invoice_code") or f"#{order.get('base_job_id')}"

        # 2. Chặn xóa nếu đơn đã hoàn thành, thất bại hoặc đã tạo phiếu bảo hành
        if st == "completed":
            return {"error": (400, "Đơn đã hoàn thành, không được phép xóa!")}
        if st not in ("pending", "processing"):
            return {
                "error": (
                    400,
                    "Chỉ được phép xóa đơn đang ở trạng thái Thông tin đơn hoặc Xử lý đơn!",
                )
            }
        if order.get("warranty_record_id") or _fetch_order_warranty_records(order):
            return {"error": (400, "Đơn này đã tạo phiếu bảo hành, không được phép xóa!")}

        # 3. Thực hiện xóa đơn khỏi bảng installation_orders
        supabase_admin.table("installation_orders").delete().eq("id", order_id).execute()
        return {"code_label": code_label}

    result = await asyncio.to_thread(_delete_tx)
    if "error" in result:
        code, msg = result["error"]
        raise HTTPException(status_code=code, detail=msg)

    return {
        "success": True,
        "message": f"Đã xóa đơn lắp đặt [{result['code_label']}]!",
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
    actor_name = _get_actor_name(user)
    update_payload = {"updated_at": now_utc, "updated_by": actor_name or None}
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
            lambda: _safe_table_update(supabase, TABLE, update_payload, "id", record_id)
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
    actor_name = _get_actor_name(user)
    payload = {
        "status": "returned",
        "returned_date": now_utc,
        "returned_by": actor_name or None,
        "updated_by": actor_name or None,
        "updated_at": now_utc,
    }

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
            lambda: _safe_table_update(supabase, TABLE, payload, "id", record_id)
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

    allowed = await asyncio.to_thread(can_access, "reception_delete", user)
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
