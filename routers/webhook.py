import asyncio
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from database import supabase_admin as supabase
from services.base_client import base_client
from utils import (
    clean_base_content,
    extract_all_base_images,
    extract_identifier_code_or_phone,
    generate_ticket_code,
    normalize_phone,
    parse_task_name,
    validate_status,
)

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/webhook", tags=["Webhook"])

ALLOWED_STATUSES = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}

_settings_cache: Optional[Dict[str, str]] = None
_settings_updated: float = 0

_status_map_cache: Optional[dict] = None
_status_map_updated: float = 0

_last_reconcile_time = {
    "repair": 0.0,
    "storage": 0.0,
    "delivery": 0.0,
}
RECONCILE_COOLDOWN = 120.0

INVALID_BASE_VALUES = {"no value", "null", "none", "0", "false", ""}
PLACEHOLDER_NOTES = {"chưa ghi chú", "chưa ghi nhận"}

_PHONE_KEYWORDS = ("dien_thoai", "dienthoai", "so_dien_thoai", "sodienthoai", "sdt", "so_dt", "phone", "mobile")
_ADDR_KEYWORDS = ("dia_chi", "diachi", "address", "dia_chi_giao_hang", "diachinhanhang", "noigiao")
_NOTE_KEYWORDS = ("ghi_chu", "ghichu", "note", "description", "mo_ta", "mota", "content", "noidung")
_SOURCE_JOB_KEYWORDS = (
    "parent_job", "source_job", "origin_job", "job_goc", "jobgoc",
    "forwarded_from", "from_job", "ma_job_goc", "source_id", "origin_id",
)
_TICKET_CODE_KEYWORDS = ("ma_phieu", "maphieu", "ticket_code", "ticketcode", "ticket", "ma_don", "madon", "code")


def clean_base_value(v) -> str:
    s = str(v if v is not None else "").strip()
    return "" if s.lower() in INVALID_BASE_VALUES else s


def _ensure_list(val) -> list:
    return val if isinstance(val, list) else []


def _normalize_id_token(raw_id) -> List[str]:
    """Chuẩn hóa ID từ Base (hỗ trợ '12345', '18190:12345', '12345,67890')."""
    s = str(raw_id or "").strip()
    if not s or s.lower() in {"0", "none", "null"}:
        return []
    out = []
    for part in re.split(r"[,;\s]+", s):
        p = part.strip()
        if not p or p.lower() in {"0", "none", "null"}:
            continue
        if p not in out:
            out.append(p)
        if ":" in p:
            tail = p.rsplit(":", 1)[-1].strip()
            if tail and tail.isdigit() and tail not in out:
                out.append(tail)
    return out


def _scan_custom_field(custom_fields: dict, keywords: tuple) -> Optional[str]:
    if not isinstance(custom_fields, dict):
        return None
    for k, v in custom_fields.items():
        if isinstance(v, (dict, list)):
            continue
        kl = str(k).lower()
        if any(kw in kl for kw in keywords):
            val = clean_base_value(v)
            if val:
                return val
    return None


def _extract_form_custom_fields(job_obj: dict) -> dict:
    """Base API /job/get thường trả custom fields trong mảng 'form'."""
    extracted = {}
    if not isinstance(job_obj, dict):
        return extracted
    form_list = job_obj.get("form")
    if isinstance(form_list, list):
        for item in form_list:
            if not isinstance(item, dict):
                continue
            k = str(item.get("id") or item.get("name") or item.get("code") or "").strip()
            v = item.get("value")
            if k and v is not None and not isinstance(v, (dict, list)):
                extracted[k] = v
    return extracted


def collect_related_job_ids(payload: dict, job_id: str, job_data: Optional[dict] = None) -> list:
    ids: List[str] = []
    sources = [payload] + ([job_data] if isinstance(job_data, dict) else [])

    def add(v):
        for token in _normalize_id_token(v):
            if token != job_id and token not in ids:
                ids.append(token)

    direct_keys = (
        "forwarded_from",
        "forwarded_from_job_id",
        "source_job_id",
        "origin_job_id",
        "parent_id",
        "parent_job_id",
        "prev_job_id",
        "prev_id",
        "root_id",
        "original_job_id",
        "from_job_id",
        "source_id",
        "origin_id",
        "ref_id",
        "link_id",
    )

    for src in sources:
        for dk in direct_keys:
            add(src.get(dk))

        for key in ("gen_prev", "gen_list"):
            raw = src.get(key)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    add(raw)
                    continue
            if isinstance(raw, dict):
                if "id" in raw:
                    add(raw.get("id"))
                items = list(raw.values())
            elif isinstance(raw, list):
                items = raw
            else:
                items = [raw]
            for g in items:
                if isinstance(g, dict):
                    add(g.get("id") or g.get("job_id"))
                elif isinstance(g, (str, int)):
                    add(g)
    return ids


def _expand_phone_variants(phone: str) -> List[str]:
    if not phone:
        return []
    digits = re.sub(r"\D", "", phone)
    national = None
    if digits.startswith("84") and len(digits) == 11:
        national = digits[2:]
    elif digits.startswith("0") and len(digits) == 10:
        national = digits[1:]
    elif len(digits) == 9:
        national = digits

    variants = {phone.strip(), digits}
    if national:
        variants.update({national, "0" + national, "84" + national, "+84" + national})
    return [v for v in variants if v]


def normalize_webhook_payload(raw_payload: dict) -> dict:
    """
    Chuyển đổi các key dạng form-urlencoded ngoặc vuông của Base.vn
    (vd: 'workflow[id]', 'stage[name]', 'job[id]') thành dict lồng nhau chuẩn.
    """
    result = dict(raw_payload)
    for key, val in raw_payload.items():
        if "[" in key and key.endswith("]"):
            parts = [p.rstrip("]") for p in key.split("[") if p.rstrip("]")]
            if len(parts) >= 2:
                curr = result
                for p in parts[:-1]:
                    if not isinstance(curr.get(p), dict):
                        curr[p] = {}
                    curr = curr[p]
                curr[parts[-1]] = val
    return result


async def safe_background_reconcile(app_name: str):
    global _last_reconcile_time
    current_time = time.time()

    if current_time - _last_reconcile_time.get(app_name, 0) < RECONCILE_COOLDOWN:
        return

    _last_reconcile_time[app_name] = current_time

    try:
        from routers.sync import reconcile_tickets

        logger.info(f"🔄 [AUTO-SYNC] Kích hoạt đối soát ngầm quét phiếu rác cho quy trình: {app_name}")
        await reconcile_tickets(app_name=app_name)
    except Exception as e:
        logger.error(f"Lỗi khi chạy đối soát ngầm cho {app_name}: {e}")


async def get_system_settings() -> Dict[str, str]:
    global _settings_cache, _settings_updated
    now = datetime.now(timezone.utc).timestamp()

    ttl = 60.0
    if _settings_cache and "cache_ttl_seconds" in _settings_cache:
        try:
            ttl = float(_settings_cache["cache_ttl_seconds"])
        except (ValueError, TypeError):
            ttl = 60.0

    if _settings_cache is not None and (now - _settings_updated) < ttl:
        return _settings_cache

    merged = {
        "workflow_repair_id": os.getenv("WORKFLOW_REPAIR_ID", "18190").strip(),
        "workflow_storage_id": os.getenv("WORKFLOW_STORAGE_ID", "18720").strip(),
        "workflow_delivery_id": os.getenv("WORKFLOW_DELIVERY_ID", "18249").strip(),
    }

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings")
            .select("key, value")
            .execute()
        )
        if res.data:
            for item in res.data:
                k = str(item.get("key") or "").strip()
                v = str(item.get("value") or "").strip()
                if k and v and v.lower() != "none":
                    merged[k] = v
            _settings_cache = merged
            _settings_updated = now
            return _settings_cache
    except Exception as e:
        logger.warning(f"Không thể tải system_settings từ DB: {e}")

    return _settings_cache or merged


async def get_dynamic_status_map() -> dict:
    global _status_map_cache, _status_map_updated
    now = datetime.now(timezone.utc).timestamp()

    settings = await get_system_settings()
    try:
        ttl = float(settings.get("cache_ttl_seconds", 60))
    except (ValueError, TypeError):
        ttl = 60.0

    if _status_map_cache is not None and (now - _status_map_updated) < ttl:
        return _status_map_cache

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("status_mappings")
            .select("base_stage, system_status")
            .execute()
        )
        if res.data:
            _status_map_cache = {
                item["base_stage"].strip().lower(): item["system_status"]
                for item in res.data
                if item.get("base_stage") and item.get("system_status") in ALLOWED_STATUSES
            }
            _status_map_updated = now
            return _status_map_cache
    except Exception as e:
        logger.warning(f"Không tải được status_mappings: {e}")

    return {
        "thông tin đơn sửa": "received",
        "tiếp nhận": "received",
        "tiến hành kiểm tra & sửa chữa": "processing",
        "đang sửa": "processing",
        "hoàn thành": "fixed",
        "trả máy": "returned",
        "hủy": "cancelled",
    }


# =========================================================
# 1. TÌM KIẾM PHIẾU GỐC TRONG CSDL
# =========================================================
async def find_existing_repair_ticket(
    candidate_job_ids: list,
    code: Optional[str],
    phone: Optional[str],
    customer_name: Optional[str] = None,
    allow_phone_match: bool = False,
) -> Optional[dict]:
    """
    Tìm phiếu đã tồn tại trong CSDL theo thứ tự ưu tiên:
    1. Danh sách Job ID (job hiện tại / job gốc trước khi forward / gen_prev).
    2. Mã phiếu (parse từ tiêu đề/ghi chú hoặc custom field).
    3. SĐT (ưu tiên phiếu đang mở -> phiếu đã trả/hủy -> fallback LIKE 9 số cuối).
    4. Tên khách hàng (chỉ áp dụng cho luồng forward khi không có SĐT/mã phiếu).
    """
    clean_ids: List[str] = []
    for raw_jid in candidate_job_ids:
        for token in _normalize_id_token(raw_jid):
            if token not in clean_ids:
                clean_ids.append(token)

    def _query() -> Optional[dict]:
        # 1. Khớp theo Job ID
        for jid in clean_ids:
            res = (
                supabase.table("repairs")
                .select("*")
                .or_(f"base_job_id.eq.{jid},storage_job_id.eq.{jid},delivery_job_id.eq.{jid}")
                .limit(1)
                .execute()
            )
            if res.data:
                logger.info(f"🔗 [MATCH-JOBID] Khớp phiếu {res.data[0].get('code')} qua job_id={jid}")
                return res.data[0]

        # 2. Khớp qua Mã phiếu
        if code:
            res_code = supabase.table("repairs").select("*").eq("code", code.upper()).limit(1).execute()
            if res_code.data:
                logger.info(f"🔗 [MATCH-CODE] Khớp phiếu {res_code.data[0].get('code')}")
                return res_code.data[0]

        # 3. Khớp theo SĐT (dành cho luồng forward hoặc workflow không rõ)
        if allow_phone_match and phone:
            phone_variants = _expand_phone_variants(phone)
            if phone_variants:
                res_active = (
                    supabase.table("repairs")
                    .select("*")
                    .in_("phone", phone_variants)
                    .not_.in_("status", ["returned", "cancelled"])
                    .order("updated_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if res_active.data:
                    logger.info(f"🔗 [MATCH-PHONE-ACTIVE] Khớp phiếu {res_active.data[0].get('code')}")
                    return res_active.data[0]

                res_closed = (
                    supabase.table("repairs")
                    .select("*")
                    .in_("phone", phone_variants)
                    .in_("status", ["returned", "cancelled"])
                    .order("updated_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if res_closed.data:
                    logger.info(
                        f"🔄 [MATCH-CLOSED] Nối tiếp phiếu đã hoàn tất/đóng sửa: "
                        f"{res_closed.data[0].get('code')}"
                    )
                    return res_closed.data[0]

                digits = re.sub(r"\D", "", phone)
                if len(digits) >= 9:
                    last9 = digits[-9:]
                    try:
                        res_like = (
                            supabase.table("repairs")
                            .select("*")
                            .like("phone", f"%{last9}%")
                            .order("updated_at", desc=True)
                            .limit(1)
                            .execute()
                        )
                        if res_like.data:
                            logger.info(
                                f"🔍 [MATCH-LIKE] Khớp phiếu qua 9 số cuối SĐT {last9}: "
                                f"{res_like.data[0].get('code')}"
                            )
                            return res_like.data[0]
                    except Exception as e:
                        logger.warning(f"[MATCH-LIKE] Query thất bại: {e}")

        # 4. Fallback cuối cho luồng forward: khớp theo Tên khách hàng khi đơn sửa chưa có SĐT
        clean_cname = (customer_name or "").strip()
        if allow_phone_match and clean_cname and clean_cname != "Khách lẻ" and len(clean_cname) >= 3:
            try:
                res_name = (
                    supabase.table("repairs")
                    .select("*")
                    .ilike("customer_name", clean_cname)
                    .is_("storage_job_id", "null")
                    .order("updated_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if res_name.data:
                    logger.info(
                        f"🔗 [MATCH-NAME] Khớp phiếu qua tên khách '{clean_cname}': "
                        f"{res_name.data[0].get('code')}"
                    )
                    return res_name.data[0]
            except Exception as e:
                logger.warning(f"[MATCH-NAME] Query thất bại: {e}")

        return None

    return await asyncio.to_thread(_query)


def get_new_images(incoming_imgs: list, *old_img_arrays) -> list:
    if not incoming_imgs:
        return []
    old_set = set()
    for arr in old_img_arrays:
        if arr and isinstance(arr, list):
            old_set.update(arr)
    return [img for img in incoming_imgs if img and img not in old_set]


def _merge_bucket_images(
    update_payload: dict,
    img_groups: dict,
    old_received: list,
    old_repair: list,
    old_storage: list,
    old_delivery: list,
) -> None:
    """Cập nhật từng rổ ảnh vào đúng cột DB tương ứng, loại bỏ ảnh đã tồn tại ở bất kỳ rổ nào."""
    all_existing = [*old_received, *old_repair, *old_storage, *old_delivery]

    new_received = get_new_images(img_groups.get("received", []), all_existing)
    if new_received:
        update_payload["received_images"] = old_received + new_received
        all_existing.extend(new_received)

    new_repair = get_new_images(img_groups.get("repair", []), all_existing)
    if new_repair:
        update_payload["repair_images"] = old_repair + new_repair
        all_existing.extend(new_repair)

    new_storage = get_new_images(img_groups.get("storage", []), all_existing)
    if new_storage:
        update_payload["storage_images"] = old_storage + new_storage
        all_existing.extend(new_storage)

    new_delivery = get_new_images(img_groups.get("delivery", []), all_existing)
    if new_delivery:
        update_payload["delivery_images"] = old_delivery + new_delivery


# =========================================================
# 2. CÁC HÀM XỬ LÝ TỪNG QUY TRÌNH (STORAGE / DELIVERY / REPAIR)
# =========================================================
async def _process_storage_webhook(
    existing_ticket: dict,
    job_id: str,
    stage_name: str,
    metatype: str,
    state: str,
    final_customer_name: str,
    final_phone: Optional[str],
    final_address: str,
    final_note: str,
    now_iso: str,
    img_groups: dict,
    old_received: list,
    old_repair: list,
    old_delivery: list,
    old_storage: list,
) -> str:
    stage_lower = stage_name.lower().strip()
    if any(kw in stage_lower for kw in ["tiêu hủy", "đã tiêu hủy", "destroy"]):
        storage_status = "destroyed"
    elif (
        metatype in {"done", "finished", "success"}
        or state in {"done", "finished", "success"}
        or any(kw in stage_lower for kw in ["gửi trả", "đã trả", "đã gửi", "done", "hoàn thành", "hoàn tất"])
    ):
        storage_status = "dispatched"
    else:
        storage_status = "in_company"

    curr_p = str(existing_ticket.get("current_process") or "").lower()
    target_storage_process = "delivery" if curr_p == "delivery" else "storage"

    # Bảo toàn dữ liệu sạch của phiếu gốc khi forward sang Kho
    safe_customer = existing_ticket.get("customer_name")
    if not safe_customer or safe_customer == "Khách lẻ":
        safe_customer = final_customer_name

    safe_phone = existing_ticket.get("phone") or final_phone
    safe_address = existing_ticket.get("address") or final_address
    safe_note = existing_ticket.get("note")
    if not safe_note or str(safe_note).strip().lower() in PLACEHOLDER_NOTES:
        safe_note = final_note

    update_payload = {
        "storage_job_id": job_id,
        "storage_status": storage_status,
        "current_process": target_storage_process,
        "customer_name": safe_customer,
        "phone": safe_phone,
        "address": safe_address,
        "note": safe_note,
        "updated_at": now_iso,
    }

    _merge_bucket_images(
        update_payload, img_groups, old_received, old_repair, old_storage, old_delivery
    )

    if storage_status == "dispatched":
        update_payload["status"] = "returned"
        update_payload["returned_date"] = now_iso
    elif storage_status == "destroyed":
        update_payload["status"] = "cancelled"
        update_payload["failure_reason"] = "Thiết bị đã tiêu hủy tại kho"
    elif str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
        update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"
        update_payload["returned_date"] = None

    await asyncio.to_thread(
        lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
    )
    ticket_code = existing_ticket.get("code")
    logger.info(f"📦 [LƯU KHO] Cập nhật phiếu {ticket_code} (storage_job_id={job_id}) -> storage_status: {storage_status}")
    return ticket_code


async def _process_delivery_webhook(
    payload: dict,
    custom_fields_data: dict,
    existing_ticket: dict,
    job_id: str,
    stage_name: str,
    metatype: str,
    state: str,
    final_customer_name: str,
    final_phone: Optional[str],
    final_address: str,
    final_note: str,
    now_iso: str,
    img_groups: dict,
    old_received: list,
    old_repair: list,
    old_delivery: list,
    old_storage: list,
) -> str:
    stage_lower = stage_name.lower().strip()

    raw_shipping_info = clean_base_value(
        payload.get("custom_thong_tin_chanh_xechuyen_phat")
        or payload.get("custom_thong_tin_chanh_xe")
        or payload.get("custom_chanh_xe")
        or payload.get("custom_dia_chi_giao_hang")
        or custom_fields_data.get("thong_tin_chanh_xe_chuyen_phat")
        or custom_fields_data.get("thong_tin_chanh_xe")
        or custom_fields_data.get("chanh_xe")
        or custom_fields_data.get("dia_chi_giao_hang")
    )
    safe_customer = existing_ticket.get("customer_name")
    if not safe_customer or safe_customer == "Khách lẻ":
        safe_customer = final_customer_name

    safe_phone = existing_ticket.get("phone") or final_phone
    delivery_address = raw_shipping_info or existing_ticket.get("address") or final_address
    safe_note = existing_ticket.get("note")
    if not safe_note or str(safe_note).strip().lower() in PLACEHOLDER_NOTES:
        safe_note = final_note

    update_payload = {
        "delivery_job_id": job_id,
        "current_process": "delivery",
        "customer_name": safe_customer,
        "phone": safe_phone,
        "address": delivery_address,
        "note": safe_note,
        "updated_at": now_iso,
    }

    if str(existing_ticket.get("storage_status") or "").lower() == "in_company":
        update_payload["storage_status"] = "dispatched"

    if any(kw in stage_lower for kw in ["thất bại", "hủy giao", "không thành công", "fail", "cancel"]):
        delivery_status = "failed"
        update_payload["delivery_status"] = "failed"
        update_payload["status"] = "cancelled"
        update_payload["is_fixed"] = False
        update_payload["repair_result"] = "failed"
        update_payload["failure_reason"] = "Giao nhận thất bại"
    elif (
        any(kw in stage_lower for kw in ["hoàn thành", "hoàn tất", "đã giao", "xong", "success", "done", "finished"])
        or metatype in {"done", "finished", "success"}
        or state in {"done", "finished", "success"}
    ):
        delivery_status = "delivered"
        update_payload["delivery_status"] = "delivered"
        update_payload["status"] = "returned"
        update_payload["returned_date"] = now_iso
        if existing_ticket.get("failure_reason") == "Giao nhận thất bại":
            update_payload["failure_reason"] = None
    elif any(kw in stage_lower for kw in ["xử lý", "đang giao", "đi giao", "vận chuyển", "delivering", "shipping", "processing"]):
        delivery_status = "delivering"
        update_payload["delivery_status"] = "delivering"
        update_payload["returned_date"] = None
        if existing_ticket.get("failure_reason") == "Giao nhận thất bại":
            update_payload["failure_reason"] = None
        if str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
            update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"
    else:
        delivery_status = "pending"
        update_payload["delivery_status"] = "pending"
        update_payload["returned_date"] = None
        if existing_ticket.get("failure_reason") == "Giao nhận thất bại":
            update_payload["failure_reason"] = None
        if str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
            update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"

    _merge_bucket_images(
        update_payload, img_groups, old_received, old_repair, old_storage, old_delivery
    )

    await asyncio.to_thread(
        lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
    )
    ticket_code = existing_ticket.get("code")
    logger.info(f"🚚 [GIAO NHẬN] Cập nhật phiếu {ticket_code} (delivery_job_id={job_id}) -> delivery_status: {delivery_status}")
    return ticket_code


async def _process_repair_webhook(
    payload: dict,
    job_data: dict,
    custom_fields_data: dict,
    existing_ticket: Optional[dict],
    job_id: str,
    stage_name: str,
    metatype: str,
    state: str,
    status_map: dict,
    final_customer_name: str,
    final_phone: Optional[str],
    final_address: str,
    final_note: str,
    final_received_note: str,
    extracted_code: Optional[str],
    now_iso: str,
    img_groups: dict,
    old_received: list,
    old_repair: list,
    old_delivery: list,
    old_storage: list,
) -> str:
    stage_for_repair = stage_name or "Tiếp nhận"
    base_failed_reason = clean_base_value(
        payload.get("failed_reason") or payload.get("fail_reason") or job_data.get("failed_reason")
    )
    base_failed_note = clean_base_value(
        payload.get("failed_note") or payload.get("fail_note") or job_data.get("failed_note")
    )
    custom_tinh_trang = clean_base_value(
        payload.get("custom_tinh_trang_thiet_bi") or custom_fields_data.get("tinh_trang_thiet_bi")
    )

    mapped_status = validate_status(stage_for_repair, status_map, metatype=metatype, state=state) or "received"

    if state == "failed" or metatype == "failed":
        mapped_status = "cancelled"

    target_process = "repair"
    if existing_ticket:
        curr_p = str(existing_ticket.get("current_process") or "repair").lower()
        if curr_p in {"storage", "delivery"}:
            target_process = curr_p

    update_payload = {
        "status": mapped_status,
        "current_process": target_process,
        "customer_name": final_customer_name,
        "phone": final_phone,
        "address": final_address,
        "note": final_note,
        "received_note": final_received_note,
        "updated_at": now_iso,
    }

    if mapped_status == "fixed":
        update_payload["is_fixed"] = True

    _merge_bucket_images(
        update_payload, img_groups, old_received, old_repair, old_storage, old_delivery
    )

    if existing_ticket:
        old_base_job_id = str(existing_ticket.get("base_job_id") or "").strip()
        if old_base_job_id != job_id:
            update_payload["base_job_id"] = job_id

        if mapped_status == "returned":
            update_payload["returned_date"] = now_iso

        if mapped_status == "cancelled":
            update_payload["is_fixed"] = False
            update_payload["repair_result"] = "failed"
            cancel_parts = []
            if base_failed_reason:
                cancel_parts.append(f"Lý do: {base_failed_reason}")
            if base_failed_note:
                cancel_parts.append(f"Ghi chú: {base_failed_note}")
            cancel_str = " - ".join(cancel_parts) if cancel_parts else "KTV báo hủy sửa"
            update_payload["failure_reason"] = cancel_str

            curr_received = (existing_ticket.get("received_note") or final_received_note or "").strip()
            if cancel_str not in curr_received:
                update_payload["received_note"] = f"{curr_received} [{cancel_str}]".strip()

        elif target_process == "repair":
            old_failure = (existing_ticket.get("failure_reason") or "").strip()
            if existing_ticket.get("repair_result") == "failed" or old_failure:
                if mapped_status != "fixed":
                    update_payload["is_fixed"] = None
                update_payload["repair_result"] = None
                update_payload["failure_reason"] = None

            rn = update_payload.get("received_note") or ""
            if old_failure:
                rn = rn.replace(f"[{old_failure}]", "")
            update_payload["received_note"] = re.sub(r"\s{2,}", " ", rn).strip()

        if mapped_status != "returned" and existing_ticket.get("returned_date"):
            update_payload["returned_date"] = None

        if base_failed_reason or base_failed_note:
            revert_parts = []
            if base_failed_reason:
                revert_parts.append(f"Lý do lùi bước: {base_failed_reason}")
            if base_failed_note:
                revert_parts.append(f"Ghi chú: {base_failed_note}")
            revert_str = " | ".join(revert_parts)
            curr_note = update_payload.get("note") or ""
            if revert_str not in curr_note:
                update_payload["note"] = f"{curr_note}\n[{revert_str}]".strip()

        if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
            ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
            curr_note = update_payload.get("note") or (existing_ticket.get("note") or "").strip()
            if ktv_text not in curr_note:
                update_payload["note"] = f"{curr_note} | {ktv_text}" if curr_note else ktv_text

        await asyncio.to_thread(
            lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
        )
        ticket_code = existing_ticket["code"]
        logger.info(f"🛠️ [SỬA CHỮA] Cập nhật phiếu {ticket_code} (Status: {mapped_status})")
        return ticket_code

    # Tạo phiếu mới
    if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
        ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
        base = update_payload.get("note") or final_note or ""
        update_payload["note"] = f"{base} | {ktv_text}" if base else ktv_text

    if extracted_code:
        ticket_code = extracted_code
    elif asyncio.iscoroutinefunction(generate_ticket_code):
        ticket_code = await generate_ticket_code()
    else:
        ticket_code = generate_ticket_code()

    new_record = {
        "base_job_id": job_id,
        "code": ticket_code,
        "customer_name": final_customer_name,
        "phone": final_phone,
        "address": final_address,
        "note": update_payload.get("note"),
        "received_note": update_payload.get("received_note"),
        "status": mapped_status,
        "current_process": "repair",
        "is_fixed": update_payload.get("is_fixed"),
        "repair_result": update_payload.get("repair_result"),
        "failure_reason": update_payload.get("failure_reason"),
        "returned_date": update_payload.get("returned_date"),
        "received_images": update_payload.get("received_images", []),
        "repair_images": update_payload.get("repair_images", []),
        "storage_images": update_payload.get("storage_images", []),
        "delivery_images": update_payload.get("delivery_images", []),
        "updated_at": now_iso,
    }
    try:
        await asyncio.to_thread(
            lambda: supabase.table("repairs").insert(new_record).execute()
        )
        logger.info(f"🛠️ [SỬA CHỮA] Tạo phiếu mới thành công: {ticket_code}")
    except Exception as insert_err:
        if "duplicate" in str(insert_err).lower():
            ex = await asyncio.to_thread(
                lambda: supabase.table("repairs").select("id, code").eq("base_job_id", job_id).limit(1).execute()
            )
            if ex.data:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", ex.data[0]["id"]).execute()
                )
                ticket_code = ex.data[0]["code"]
            else:
                raise
        else:
            raise

    return ticket_code


def _extract_wf_id(payload: dict, job_data: dict, workflow_obj: dict, workflow_storage: str, workflow_delivery: str) -> str:
    """Trích xuất workflow_id chuẩn xác, ưu tiên target workflow nếu là sự kiện forward."""
    target_wf = str(
        payload.get("target_container_id")
        or payload.get("destination_workflow_id")
        or payload.get("to_workflow_id")
        or job_data.get("target_container_id")
        or ""
    ).strip()
    if target_wf in {workflow_storage, workflow_delivery}:
        return target_wf

    raw_wf = (
        payload.get("workflow_id")
        or job_data.get("workflow_id")
        or job_data.get("wf_id")
        or (workflow_obj.get("id") if isinstance(workflow_obj, dict) else None)
        or target_wf
        or payload.get("container_id")
        or payload.get("source_container_id")
        or ""
    )
    return str(raw_wf).strip()


# =========================================================
# 3. ENDPOINT DUY NHẤT NHẬN WEBHOOK TỪ BASE.VN
# =========================================================
@router.post("/base")
async def receive_base_webhook(request: Request, bg_tasks: BackgroundTasks):
    try:
        settings = await get_system_settings()
        workflow_repair = str(settings.get("workflow_repair_id") or settings.get("workflow_repair") or "18190").strip()
        workflow_storage = str(settings.get("workflow_storage_id") or settings.get("workflow_storage") or "18720").strip()
        workflow_delivery = str(settings.get("workflow_delivery_id") or settings.get("workflow_delivery") or "18249").strip()
        configured_secret = settings.get("base_shared_secret", "")

        # 1. Parse Request Body & chuẩn hóa key ngoặc vuông từ Base form-urlencoded
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            raw_payload = await request.json()
        else:
            form_data = await request.form()
            raw_payload = dict(form_data)

        payload = normalize_webhook_payload(raw_payload)

        if "custom_fields" in payload and isinstance(payload["custom_fields"], str):
            try:
                payload["custom_fields"] = json.loads(payload["custom_fields"])
            except Exception:
                pass

        job_data = payload.get("job") if isinstance(payload.get("job"), dict) else {}

        # 2. Xác thực Secret Token
        incoming_secret = str(
            request.headers.get("x-base-secret")
            or request.query_params.get("secret")
            or payload.get("secret")
            or ""
        )
        if configured_secret and not hmac.compare_digest(incoming_secret.encode(), configured_secret.encode()):
            logger.warning("Webhook: Secret Token không khớp hoặc bị thiếu")
            raise HTTPException(status_code=401, detail="Xác thực không hợp lệ")

        # 3. Trích xuất Job ID, Task Name, Workflow ID, Stage
        job_id = str(payload.get("job_id") or payload.get("id") or job_data.get("id") or "").strip()
        if not job_id or job_id.lower() == "none":
            return {"status": "ignored", "message": "Thiếu job_id hợp lệ", "code": None}

        workflow_obj = payload.get("workflow") or job_data.get("workflow") or {}
        if isinstance(workflow_obj, str):
            try:
                workflow_obj = json.loads(workflow_obj)
            except Exception:
                workflow_obj = {}

        workflow_id = _extract_wf_id(payload, job_data, workflow_obj, workflow_storage, workflow_delivery)
        workflow_install = str(
            settings.get("workflow_install_id") or settings.get("workflow_install") or "18191"
        ).strip()
        # Nếu là quy trình Kho/Giao nhận hoặc thiếu workflow_id: gọi thêm Base API /job/get để lấy full gen_prev + custom_fields
        need_api_enrich = (
            not workflow_id
            or workflow_id in {workflow_storage, workflow_delivery}
            or not (payload.get("gen_prev") or payload.get("parent_id") or job_data.get("gen_prev"))
        )
        if need_api_enrich and workflow_id not in {workflow_repair, workflow_install}:
            preferred = (
                ["storage", "delivery", "repair"]
                if workflow_id != workflow_delivery
                else ["delivery", "storage", "repair"]
            )
            fetched_job = await base_client.fetch_job_detail(job_id, preferred_apps=preferred)
            if isinstance(fetched_job, dict):
                job_data = {**fetched_job, **job_data}
                if not workflow_id:
                    workflow_id = _extract_wf_id(payload, job_data, workflow_obj, workflow_storage, workflow_delivery)

        original_workflow_id = workflow_id

        task_name = str(
            payload.get("task_name")
            or payload.get("name")
            or payload.get("job_name")
            or job_data.get("name")
            or job_data.get("title")
            or ""
        ).strip()

        state = str(payload.get("state") or job_data.get("state") or "").strip().lower()

        stage_obj = payload.get("stage") or job_data.get("stage") or {}
        if isinstance(stage_obj, str):
            try:
                stage_obj = json.loads(stage_obj)
            except Exception:
                stage_obj = {}

        if isinstance(stage_obj, dict):
            stage_name = str(stage_obj.get("name") or payload.get("stage_name") or "").strip()
            metatype = str(stage_obj.get("metatype") or payload.get("metatype") or "").strip().lower()
        else:
            stage_name = str(payload.get("stage_name") or stage_obj or "").strip()
            metatype = str(payload.get("metatype") or "").strip().lower()

        logger.info(
            f"⚡ [WEBHOOK] WF: '{workflow_id}' (RepairCfg='{workflow_repair}', StorageCfg='{workflow_storage}', DeliveryCfg='{workflow_delivery}') | "
            f"Stage: '{stage_name}' | Job: '{job_id}' | Name: '{task_name}'"
        )

        # 4. Bóc tách Custom Fields & thông tin khách hàng
        job_cf = job_data.get("custom_fields")
        if isinstance(job_cf, str):
            try:
                job_cf = json.loads(job_cf)
            except Exception:
                job_cf = {}

        form_cf = _extract_form_custom_fields(job_data)

        root_custom_fields = {
            k[7:]: v
            for k, v in payload.items()
            if str(k).startswith("custom_")
            and not str(k).endswith(("-fids", "-direct_links", "-names"))
            and not isinstance(v, (dict, list))
        }

        custom_fields_data = {
            **form_cf,
            **root_custom_fields,
            **(job_cf if isinstance(job_cf, dict) else {}),
            **(payload.get("custom_fields") if isinstance(payload.get("custom_fields"), dict) else {}),
        }

        scanned_phone = _scan_custom_field(custom_fields_data, _PHONE_KEYWORDS)
        scanned_addr = _scan_custom_field(custom_fields_data, _ADDR_KEYWORDS)
        scanned_note = _scan_custom_field(custom_fields_data, _NOTE_KEYWORDS)
        scanned_source_job = _scan_custom_field(custom_fields_data, _SOURCE_JOB_KEYWORDS)
        scanned_ticket_code = _scan_custom_field(custom_fields_data, _TICKET_CODE_KEYWORDS)

        raw_addr_field = clean_base_value(
            payload.get("custom_dia_chi") or custom_fields_data.get("dia_chi") or scanned_addr
        )
        raw_note_field = clean_base_value(
            payload.get("custom_ghi_chu")
            or custom_fields_data.get("ghi_chu")
            or payload.get("description")
            or job_data.get("description")
            or scanned_note
        )
        raw_phone_field = clean_base_value(
            payload.get("custom_dien_thoai")
            or payload.get("custom_so_dien_thoai")
            or payload.get("phone")
            or custom_fields_data.get("dien_thoai")
            or custom_fields_data.get("so_dien_thoai")
            or scanned_phone
        )
        raw_content_blob = " ".join(
            str(x).strip()
            for x in (
                payload.get("content_short"),
                payload.get("content"),
                payload.get("note"),
                job_data.get("content_short"),
                job_data.get("content"),
            )
            if x and str(x).strip()
        )
        content_fallback = clean_base_content(raw_content_blob)

        combined_parts = [p for p in (raw_addr_field, raw_note_field) if p]
        base_note = "\n".join(combined_parts) if combined_parts else content_fallback

        # Quét Mã phiếu & SĐT từ chuỗi THÔ trước khi strip_base_boilerplate xóa nhãn
        raw_search_blob = " ".join(
            part for part in (
                task_name,
                raw_phone_field,
                raw_addr_field,
                raw_note_field,
                raw_content_blob,
                json.dumps(custom_fields_data, ensure_ascii=False) if custom_fields_data else "",
            ) if part
        )

        parsed_info = parse_task_name(task_name, base_note)
        extracted_code, extracted_phone_title = extract_identifier_code_or_phone(raw_search_blob)
        extracted_phone_norm = (
            normalize_phone(raw_phone_field)
            or normalize_phone(parsed_info.get("phone", "").strip())
            or normalize_phone(extracted_phone_title or "")
            or None
        )
       

        # Rẽ nhánh riêng cho Quy trình Kỹ Thuật Lắp Đặt -> Không đi qua bảng repairs
        # Rẽ nhánh riêng cho Quy trình Kỹ Thuật Lắp Đặt -> Lưu vào bảng installation_orders
        if workflow_id == workflow_install:
            install_ref = await _process_installation_webhook(
                payload=payload,
                job_data=job_data,
                job_id=job_id,
                task_name=task_name,
                stage_name=stage_name,
                metatype=metatype,
                state=state,
                custom_fields_data=custom_fields_data,
            )
            logger.info(
                f"🛠️ [INSTALL-WEBHOOK] Đồng bộ thành công Job={job_id} | Stage='{stage_name}' | Ref='{install_ref}'"
            )
            return {
                "status": "success",
                "message": "Đã đồng bộ dữ liệu quy trình Lắp đặt",
                "workflow_id": workflow_id,
                "code": install_ref,
                "stage_name": stage_name or "Thông tin đơn",
            }

        # 5. Tìm phiếu đã tồn tại trong CSDL (Siết chặt liên kết để tránh cướp phiếu cũ qua SĐT)
        related_ids = collect_related_job_ids(payload, job_id, job_data)
        if scanned_source_job:
            for tok in _normalize_id_token(scanned_source_job):
                if tok != job_id and tok not in related_ids:
                    related_ids.append(tok)

        final_code_for_match = extracted_code or scanned_ticket_code
        extracted_name = parsed_info.get("customer_name", "").strip()

        # Chỉ cho phép ghép phụ theo SĐT khi đơn có dấu hiệu chuyển tiếp rõ ràng
        # (có related_ids/gen_prev, mã phiếu gốc, source_job hoặc tag [FWD])
        has_explicit_forward_link = (
            bool(related_ids)
            or bool(scanned_source_job)
            or bool(final_code_for_match)
            or "[fwd]" in task_name.lower()
        )

        existing_ticket = await find_existing_repair_ticket(
            candidate_job_ids=[job_id, *related_ids],
            code=final_code_for_match,
            phone=extracted_phone_norm,
            customer_name=extracted_name,
            allow_phone_match=has_explicit_forward_link,
        )

        # Chuẩn hóa Workflow ID nếu Base gửi thiếu (KHÔNG bao giờ ép job forward mới về repair)
        if existing_ticket:
            stored_base_job_id = str(existing_ticket.get("base_job_id") or "").strip()
            stored_delivery_job_id = str(existing_ticket.get("delivery_job_id") or "").strip()
            stored_storage_job_id = str(existing_ticket.get("storage_job_id") or "").strip()

            if workflow_id not in {workflow_storage, workflow_delivery}:
                if stored_delivery_job_id and job_id == stored_delivery_job_id:
                    workflow_id = workflow_delivery
                elif stored_storage_job_id and job_id == stored_storage_job_id:
                    workflow_id = workflow_storage
                elif not workflow_id and job_id != stored_base_job_id:
                    # Đây là job_id MỚI gắn vào phiếu đã có -> chắc chắn là chuyển tiếp (Forward)
                    st_lower = stage_name.lower()
                    if any(kw in st_lower for kw in ["giao", "vận chuyển", "chành xe", "ship", "delivery"]):
                        workflow_id = workflow_delivery
                    else:
                        workflow_id = workflow_storage

            if not workflow_id:
                workflow_id = workflow_repair

        # Guard Clause: Chỉ cho phép tạo phiếu mới khi workflow_id CHÍNH XÁC là quy trình sửa chữa
        if not existing_ticket:
            if original_workflow_id == workflow_repair:
                logger.info(f"🆕 [NEW-TICKET] Workflow sửa chữa xác nhận, sẽ tạo phiếu mới. Job={job_id}")
            else:
                logger.warning(
                    f"⛔ [BLOCKED] Không nối được phiếu gốc. "
                    f"WF_orig='{original_workflow_id}' WF_norm='{workflow_id}' Job='{job_id}' "
                    f"RelatedIDs={related_ids} Code='{final_code_for_match}' "
                    f"Phone='{extracted_phone_norm}' Name='{extracted_name}' Task='{task_name}'"
                )
                return {
                    "status": "ignored",
                    "message": "Không tìm thấy phiếu sửa chữa gốc để chuyển tiếp (đã thử JobID/GenPrev/Mã phiếu/SĐT/Tên).",
                    "code": None,
                }

        # 6. Hợp nhất dữ liệu & trích xuất hình ảnh theo từng rổ giai đoạn
        now_iso = datetime.now(timezone.utc).isoformat()
        extracted_addr = parsed_info.get("address", "").strip() or raw_addr_field
        extracted_req_note = parsed_info.get("note", "").strip() or raw_note_field
        extracted_rec_note = parsed_info.get("received_note", "").strip()
        req_note_is_real = bool(extracted_req_note) and extracted_req_note.lower() not in PLACEHOLDER_NOTES

        if existing_ticket:
            final_customer_name = (
                extracted_name
                if (extracted_name and extracted_name != "Khách lẻ")
                else existing_ticket.get("customer_name")
            )
            final_phone = extracted_phone_norm if extracted_phone_norm else existing_ticket.get("phone")
            final_address = extracted_addr if extracted_addr else existing_ticket.get("address")
            final_note = extracted_req_note if req_note_is_real else existing_ticket.get("note")
            final_received_note = extracted_rec_note if extracted_rec_note else existing_ticket.get("received_note")
        else:
            final_customer_name = extracted_name or "Khách lẻ"
            final_phone = extracted_phone_norm
            final_address = extracted_addr
            final_note = extracted_req_note if req_note_is_real else "Chưa ghi chú"
            final_received_note = extracted_rec_note

        old_received = _ensure_list(existing_ticket.get("received_images") if existing_ticket else None)
        old_repair = _ensure_list(existing_ticket.get("repair_images") if existing_ticket else None)
        old_delivery = _ensure_list(existing_ticket.get("delivery_images") if existing_ticket else None)
        old_storage = _ensure_list(existing_ticket.get("storage_images") if existing_ticket else None)

        status_map = await get_dynamic_status_map()
        preview_stage_status = validate_status(
            stage_name or "Tiếp nhận", status_map, metatype=metatype, state=state
        ) or "received"

        # Merge thêm job_data vào payload để trích xuất cả ảnh nằm trong job_data trả về từ Base API
        merged_image_payload = {**job_data, **payload}
        img_groups, _ = extract_all_base_images(
            merged_image_payload,
            workflow_id,
            {**settings, "workflow_storage_id": workflow_storage, "workflow_delivery_id": workflow_delivery},
            stage_status=preview_stage_status,
            has_existing_received=bool(old_received),
        )

        # 7. Điều hướng cập nhật CSDL theo từng quy trình (khóa chặt nhánh repair)
        if workflow_id == workflow_storage:
            ticket_code = await _process_storage_webhook(
                existing_ticket=existing_ticket,
                job_id=job_id,
                stage_name=stage_name,
                metatype=metatype,
                state=state,
                final_customer_name=final_customer_name,
                final_phone=final_phone,
                final_address=final_address,
                final_note=final_note,
                now_iso=now_iso,
                img_groups=img_groups,
                old_received=old_received,
                old_repair=old_repair,
                old_delivery=old_delivery,
                old_storage=old_storage,
            )
            current_app_name = "storage"

        elif workflow_id == workflow_delivery:
            ticket_code = await _process_delivery_webhook(
                payload=payload,
                custom_fields_data=custom_fields_data,
                existing_ticket=existing_ticket,
                job_id=job_id,
                stage_name=stage_name,
                metatype=metatype,
                state=state,
                final_customer_name=final_customer_name,
                final_phone=final_phone,
                final_address=final_address,
                final_note=final_note,
                now_iso=now_iso,
                img_groups=img_groups,
                old_received=old_received,
                old_repair=old_repair,
                old_delivery=old_delivery,
                old_storage=old_storage,
            )
            current_app_name = "delivery"

        elif workflow_id == workflow_repair:
            ticket_code = await _process_repair_webhook(
                payload=payload,
                job_data=job_data,
                custom_fields_data=custom_fields_data,
                existing_ticket=existing_ticket,
                job_id=job_id,
                stage_name=stage_name,
                metatype=metatype,
                state=state,
                status_map=status_map,
                final_customer_name=final_customer_name,
                final_phone=final_phone,
                final_address=final_address,
                final_note=final_note,
                final_received_note=final_received_note,
                extracted_code=extracted_code,
                now_iso=now_iso,
                img_groups=img_groups,
                old_received=old_received,
                old_repair=old_repair,
                old_delivery=old_delivery,
                old_storage=old_storage,
            )
            current_app_name = "repair"

        else:
            logger.warning(f"⛔ [BLOCKED] Workflow không hợp lệ: WF='{workflow_id}' | Job='{job_id}'")
            return {
                "status": "ignored",
                "message": f"Workflow '{workflow_id}' không được phép ghi dữ liệu.",
                "code": None,
            }

        # 8. Kích hoạt đối soát ngầm & phản hồi
        bg_tasks.add_task(safe_background_reconcile, current_app_name)

        return {
            "status": "success",
            "message": "Đồng bộ webhook thành công (Đã kích hoạt quét rác ngầm)",
            "workflow_id": workflow_id,
            "code": ticket_code,
            "stage_name": stage_name or "Tiếp nhận",
        }

    except HTTPException:
        raise
    except Exception as e:
        if "BLOCKED_NON_REPAIR_ORIGIN" in str(e):
            logger.warning(f"⛔ [DB-TRIGGER-BLOCKED] {str(e)}")
            return {
                "status": "ignored",
                "message": "Database chặn đơn không xuất phát từ quy trình Sửa chữa.",
                "code": None,
            }
        logger.error(f"[Webhook Error] {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xử lý webhook")

  # --- XỬ LÝ DÀNH CHO QUY TRÌNH LẮP ĐẶT --- #
def _map_installation_stage_status(stage_name: str, metatype: str = "", state: str = "") -> str:

    nfd = unicodedata.normalize("NFD", str(stage_name or ""))
    st = (
        "".join(ch for ch in nfd if unicodedata.category(ch) != "Mn")
        .replace("đ", "d")
        .replace("Đ", "D")
        .lower()
        .strip()
    )
    mt = (metatype or "").lower().strip()
    stt = (state or "").lower().strip()

    if mt == "failed" or stt == "failed" or any(k in st for k in ["that bai", "huy"]):
        return "failed"
    if mt == "done" or stt == "done" or any(k in st for k in ["hoan thanh", "da lap", "xong"]):
        return "completed"
    if any(k in st for k in ["dang xu ly", "xu ly don", "dang lap", "trien khai"]):
        return "processing"
    return "pending"


def _extract_installation_data(payload: dict, job_data: dict, task_name: str, custom_fields_data: dict) -> dict:
    # 1. Tách Mã hóa đơn (VD: HD038906) và Model gợi ý ngay sau mã HD (VD: L18050, MF 463DW, AC 450)
    invoice_code = ""
    model_hint = ""
    hd_match = re.search(
        r"\b(HD\d+)\b\s*([A-Z0-9][A-Z0-9\-]*(?:\s+\d+[A-Z0-9]*)?)?",
        task_name,
        flags=re.IGNORECASE,
    )
    if hd_match:
        invoice_code = (hd_match.group(1) or "").upper()
        raw_model = (hd_match.group(2) or "").strip()
        # Loại bỏ các từ khóa giao nhận không phải tên model
        if raw_model and raw_model.upper() not in {"GHLD", "GHLĐ", "XE", "NHA", "GUI", "GỬI", "MUA", "DOI", "ĐỢI"}:
            model_hint = raw_model.upper()

    # 2. Lấy thông tin từ trường nhập tay (Khách Mới)
    customer_name = clean_base_value(
        payload.get("custom_ten_khach_hang") or custom_fields_data.get("ten_khach_hang")
    )
    phone_raw = clean_base_value(
        payload.get("custom_dien_thoai") or custom_fields_data.get("dien_thoai")
    )
    address = clean_base_value(
        payload.get("custom_dia_chi") or custom_fields_data.get("dia_chi")
    )
    shipping_info = clean_base_value(
        payload.get("custom_thong_tin_chanh_xechuyen_phat")
        or custom_fields_data.get("thong_tin_chanh_xechuyen_phat")
    )
    customer_type = clean_base_value(
        payload.get("custom_phan_loai_khach_hang")
        or custom_fields_data.get("phan_loai_khach_hang")
    )

    # 3. Khách Quen -> Ưu tiên đọc JSON table_custom_khach_hang
    table_kh = payload.get("table_custom_khach_hang") or job_data.get("table_custom_khach_hang")
    if isinstance(table_kh, str) and table_kh.strip().startswith("{"):
        try:
            table_kh = json.loads(table_kh)
        except Exception:
            table_kh = {}

    if isinstance(table_kh, dict) and table_kh:
        customer_name = customer_name or clean_base_value(table_kh.get("1_1"))
        phone_raw = phone_raw or clean_base_value(table_kh.get("1_2"))
        address = address or clean_base_value(table_kh.get("1_3"))

    # 4. Fallback: Tách từ chuỗi keyword ngăn cách bằng dấu "|"
    if not (customer_name and phone_raw and address):
        kw_raw = str(payload.get("keyword") or job_data.get("keyword") or "")
        if "|" in kw_raw:
            parts = [p.strip() for p in kw_raw.split("|")]
            for idx, part in enumerate(parts):
                digits = re.sub(r"\D", "", part)
                if len(digits) in (10, 11) and digits.startswith(("0", "84")):
                    if not phone_raw:
                        phone_raw = part
                    if not customer_name and idx >= 1:
                        prev_val = parts[idx - 1].split("&middot;")[-1].strip()
                        if prev_val and not prev_val.startswith("{"):
                            customer_name = prev_val
                    if not address and idx + 1 < len(parts):
                        next_val = parts[idx + 1].strip()
                        if next_val and not next_val.startswith("{"):
                            address = next_val
                    break

    # 5. Fallback tên khách quen từ "Trần Hữu Tài (#199051)" hoặc SĐT trên tiêu đề
    if not customer_name:
        ref_kh = clean_base_value(payload.get("custom_khach_hang") or custom_fields_data.get("khach_hang"))
        if ref_kh:
            customer_name = re.sub(r"\s*\(#\d+\)\s*$", "", ref_kh).strip()

    if not phone_raw:
        phone_in_title = re.search(r"(0\d{2,3}[\s\.\-]?\d{3}[\s\.\-]?\d{3,4})", task_name)
        if phone_in_title:
            phone_raw = phone_in_title.group(1)

    # 6. Lấy Số Serial từ custom_so_seria_may & custom_so_seria_may_2 (dùng chung cho Khách Mới & Khách Quen)
    sn1 = clean_base_value(
        payload.get("custom_so_seria_may")
        or custom_fields_data.get("so_seria_may")
        or payload.get("custom_so_serial")
        or payload.get("custom_serial")
        or custom_fields_data.get("so_serial")
        or custom_fields_data.get("serial")
    )
    sn2 = clean_base_value(
        payload.get("custom_so_seria_may_2")
        or custom_fields_data.get("so_seria_may_2")
    )

    serial_list = []
    for sn in (sn1, sn2):
        sn_upper = sn.upper()
        if sn_upper and sn_upper not in serial_list:
            serial_list.append(sn_upper)
    serial_number = ", ".join(serial_list)

    assignee_obj = payload.get("assignee") or job_data.get("assignee") or {}
    if isinstance(assignee_obj, str) and assignee_obj.strip().startswith("{"):
        try:
            assignee_obj = json.loads(assignee_obj)
        except Exception:
            assignee_obj = {}
    assignee_name = assignee_obj.get("name", "") if isinstance(assignee_obj, dict) else ""

    return {
        "invoice_code": invoice_code,
        "task_name": task_name,
        "customer_type": customer_type or "Khách Mới",
        "customer_name": customer_name or "Khách lẻ",
        "phone": normalize_phone(phone_raw) or phone_raw or "",
        "address": address or "",
        "note": shipping_info or "",
        "model_hint": model_hint,
        "serial_number": serial_number,
        "assignee_name": assignee_name,
    }


async def _process_installation_webhook(
    payload: dict,
    job_data: dict,
    job_id: str,
    task_name: str,
    stage_name: str,
    metatype: str,
    state: str,
    custom_fields_data: dict,
):
    info = _extract_installation_data(payload, job_data, task_name, custom_fields_data)
    mapped_status = _map_installation_stage_status(stage_name, metatype, state)
    now_iso = datetime.now(timezone.utc).isoformat()

    existing_res = await asyncio.to_thread(
        lambda: supabase.table("installation_orders")
        .select("*")
        .eq("base_job_id", job_id)
        .limit(1)
        .execute()
    )
    existing = (existing_res.data or [None])[0]

    # Nếu đơn bị lưu trữ (Archive) trên Base và chưa cấp phiếu bảo hành -> dọn khỏi bảng Kanban
    is_archived = str(payload.get("is_archive") or job_data.get("is_archive") or "0").strip() == "1"
    if is_archived:
        if existing and not existing.get("warranty_record_id"):
            await asyncio.to_thread(
                lambda: supabase.table("installation_orders").delete().eq("id", existing["id"]).execute()
            )
        return (existing.get("invoice_code") if existing else info["invoice_code"]) or job_id

    if existing:
        # Không ghi đè dữ liệu KTV đã chuẩn hóa trên App nếu Base gửi lại rỗng hoặc dữ liệu cũ
        update_payload = {
            "status": mapped_status,
            "base_stage_name": stage_name or existing.get("base_stage_name"),
            "task_name": task_name or existing.get("task_name"),
            "updated_at": now_iso,
        }
        if info["assignee_name"]:
            update_payload["assignee_name"] = info["assignee_name"]
        if not existing.get("invoice_code") and info["invoice_code"]:
            update_payload["invoice_code"] = info["invoice_code"]
        if (not existing.get("customer_name") or existing.get("customer_name") == "Khách lẻ") and info["customer_name"] != "Khách lẻ":
            update_payload["customer_name"] = info["customer_name"]
        if not existing.get("phone") and info["phone"]:
            update_payload["phone"] = info["phone"]
        if not existing.get("address") and info["address"]:
            update_payload["address"] = info["address"]
        if not existing.get("model_hint") and info["model_hint"]:
            update_payload["model_hint"] = info["model_hint"]
        # Cho phép đồng bộ cập nhật/bổ sung Serial từ Base miễn là đơn chưa tạo phiếu bảo hành
        if info["serial_number"] and not existing.get("warranty_record_id"):
            update_payload["serial_number"] = info["serial_number"]

        await asyncio.to_thread(
            lambda: supabase.table("installation_orders").update(update_payload).eq("id", existing["id"]).execute()
        )
        return existing.get("invoice_code") or job_id

    insert_payload = {
        "base_job_id": job_id,
        "invoice_code": info["invoice_code"] or None,
        "task_name": info["task_name"],
        "customer_type": info["customer_type"],
        "customer_name": info["customer_name"],
        "phone": info["phone"] or None,
        "address": info["address"] or None,
        "model_hint": info["model_hint"] or None,
        "serial_number": info["serial_number"] or None,
        "note": info["note"] or None,
        "status": mapped_status,
        "base_stage_name": stage_name or "Thông tin đơn",
        "assignee_name": info["assignee_name"] or None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await asyncio.to_thread(
        lambda: supabase.table("installation_orders").insert(insert_payload).execute()
    )
    return info["invoice_code"] or job_id
