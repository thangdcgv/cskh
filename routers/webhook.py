import asyncio
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from database import supabase
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

# =========================================================
# [FIX] Từ khóa quét custom field (input key mỗi workflow có thể khác nhau)
# =========================================================
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


# =========================================================
# [FIX] Helper 1: quét custom field theo từ khóa (bỏ qua giá trị dict/list)
# =========================================================
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

def collect_related_job_ids(payload: dict, job_id: str, job_data: Optional[dict] = None) -> list:
    ids = []
    sources = [payload] + ([job_data] if isinstance(job_data, dict) else [])

    def add(v):
        s = str(v or "").strip()
        if s and s not in {"0", "none", "None", job_id} and s not in ids:
            ids.append(s)

    for src in sources:
        add(src.get("forwarded_from"))
        add(src.get("source_job_id"))
        for key in ("gen_prev", "gen_list"):
            raw = src.get(key)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    add(raw)
                    continue
            items = raw if isinstance(raw, list) else [raw]
            for g in items:
                if isinstance(g, dict):
                    add(g.get("id"))
                elif isinstance(g, (str, int)):
                    add(g)
    return ids
# =========================================================
# [FIX] Helper 2: sinh đủ biến thể định dạng SĐT để khớp với DB
# =========================================================
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

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings")
            .select("key, value")
            .execute()
        )
        if res.data:
            _settings_cache = {str(item["key"]).strip(): str(item["value"]).strip() for item in res.data}
            _settings_updated = now
            return _settings_cache
    except Exception as e:
        logger.warning(f"Không thể tải system_settings từ DB: {e}")

    return _settings_cache or {}


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
    allow_phone_match: bool = False,
) -> Optional[dict]:
    """
    Tìm phiếu đã tồn tại trong CSDL theo thứ tự ưu tiên:
    1. Danh sách Job ID (job hiện tại / job gốc trước khi forward / job nguồn).
    2. Mã phiếu (parse từ tiêu đề/ghi chú hoặc custom field).
    3. SĐT (ưu tiên phiếu đang mở -> phiếu đã trả/hoàn thành -> fallback LIKE 9 số cuối).
    """
    clean_ids = [
        str(jid).strip()
        for jid in dict.fromkeys(candidate_job_ids)
        if str(jid).strip().lower() not in ("", "none", "null")
    ]

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
                return res.data[0]

        # 2. Khớp qua Mã phiếu
        if code:
            res_code = supabase.table("repairs").select("*").eq("code", code).limit(1).execute()
            if res_code.data:
                return res_code.data[0]

        # 3. Khớp theo SĐT (dành cho luồng forward hoặc workflow không rõ)
        if allow_phone_match and phone:
            phone_variants = _expand_phone_variants(phone)
            if phone_variants:
                # 3a. Ưu tiên phiếu đang mở
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
                    return res_active.data[0]

                # 3b. Fallback: Tìm phiếu vừa hoàn thành/trả máy (returned) để nối sang Lưu kho/Giao nhận
                res_returned = (
                    supabase.table("repairs")
                    .select("*")
                    .in_("phone", phone_variants)
                    .eq("status", "returned")
                    .order("updated_at", desc=True)
                    .limit(1)
                    .execute()
                )
                if res_returned.data:
                    logger.info(
                        f"🔄 [MATCH-RETURNED] Nối tiếp phiếu đã trả/hoàn thành: "
                        f"{res_returned.data[0].get('code')}"
                    )
                    return res_returned.data[0]

                # 3c. Cuối cùng: LIKE 9 số cuối (chỉ quét phiếu đang mở để tránh nhầm lẫn)
                digits = re.sub(r"\D", "", phone)
                if len(digits) >= 9:
                    last9 = digits[-9:]
                    try:
                        res_like = (
                            supabase.table("repairs")
                            .select("*")
                            .like("phone", f"%{last9}%")
                            .not_.in_("status", ["returned", "cancelled"])
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

    update_payload = {
        "storage_job_id": job_id,
        "storage_status": storage_status,
        "current_process": target_storage_process,
        "customer_name": final_customer_name,
        "phone": final_phone,
        "address": final_address,
        "note": final_note,
        "updated_at": now_iso,
    }

    new_storage_imgs = get_new_images(
        img_groups.get("storage", []), old_received, old_repair, old_delivery, old_storage
    )
    if new_storage_imgs:
        update_payload["storage_images"] = old_storage + new_storage_imgs

    if storage_status == "dispatched":
        update_payload["status"] = "returned"
        update_payload["returned_date"] = now_iso
    elif storage_status == "destroyed":
        update_payload["status"] = "cancelled"
        update_payload["failure_reason"] = "Thiết bị đã tiêu hủy tại kho"
    elif str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
        update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"

    await asyncio.to_thread(
        lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
    )
    ticket_code = existing_ticket.get("code")
    logger.info(f"📦 [LƯU KHO] Cập nhật phiếu {ticket_code} -> storage_status: {storage_status}")
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
    delivery_address = raw_shipping_info or final_address

    update_payload = {
        "delivery_job_id": job_id,
        "current_process": "delivery",
        "customer_name": final_customer_name,
        "phone": final_phone,
        "address": delivery_address,
        "note": final_note,
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
        if existing_ticket.get("failure_reason") == "Giao nhận thất bại":
            update_payload["failure_reason"] = None
        if str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
            update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"
    else:
        delivery_status = "pending"
        update_payload["delivery_status"] = "pending"
        if existing_ticket.get("failure_reason") == "Giao nhận thất bại":
            update_payload["failure_reason"] = None
        if str(existing_ticket.get("status") or "").lower() in {"returned", "cancelled"}:
            update_payload["status"] = "fixed" if existing_ticket.get("is_fixed") else "received"

    new_delivery_imgs = get_new_images(
        img_groups.get("delivery", []), old_received, old_repair, old_delivery, old_storage
    )
    if new_delivery_imgs:
        update_payload["delivery_images"] = old_delivery + new_delivery_imgs

    await asyncio.to_thread(
        lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
    )
    ticket_code = existing_ticket.get("code")
    logger.info(f"🚚 [GIAO NHẬN] Cập nhật phiếu {ticket_code} -> delivery_status: {delivery_status}")
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

    status_map = await get_dynamic_status_map()
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

    if existing_ticket:
        old_base_job_id = str(existing_ticket.get("base_job_id") or "").strip()
        if old_base_job_id != job_id:
            update_payload["base_job_id"] = job_id

        repair_imgs = img_groups.get("repair", [])
        if mapped_status == "received":
            new_received_imgs = get_new_images(
                repair_imgs, old_received, old_repair, old_delivery, old_storage
            )
            if new_received_imgs:
                update_payload["received_images"] = old_received + new_received_imgs
        else:
            new_repair_imgs = get_new_images(
                repair_imgs, old_received, old_repair, old_delivery, old_storage
            )
            if new_repair_imgs:
                update_payload["repair_images"] = old_repair + new_repair_imgs

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
        "delivery_images": [],
        "storage_images": [],
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


# =========================================================
# 3. ENDPOINT NHẬN WEBHOOK TỪ BASE.VN
# =========================================================
@router.post("/base")
async def receive_base_webhook(request: Request, bg_tasks: BackgroundTasks):
    try:
        settings = await get_system_settings()
        workflow_repair = str(settings.get("workflow_repair_id") or settings.get("workflow_repair", "18190")).strip()
        workflow_storage = str(settings.get("workflow_storage_id") or settings.get("workflow_storage", "18720")).strip()
        workflow_delivery = str(settings.get("workflow_delivery_id") or settings.get("workflow_delivery", "18249")).strip()
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

        task_name = str(
            payload.get("task_name")
            or payload.get("name")
            or payload.get("job_name")
            or job_data.get("name")
            or job_data.get("title")
            or ""
        ).strip()

        state = str(payload.get("state") or job_data.get("state") or "").strip().lower()

        workflow_obj = payload.get("workflow") or job_data.get("workflow") or {}
        if isinstance(workflow_obj, str):
            try:
                workflow_obj = json.loads(workflow_obj)
            except Exception:
                workflow_obj = {}

        raw_wf_id = (
            payload.get("workflow_id")
            or job_data.get("workflow_id")
            or job_data.get("wf_id")
            or (workflow_obj.get("id") if isinstance(workflow_obj, dict) else None)
            or payload.get("target_container_id")
            or payload.get("container_id")
            or payload.get("source_container_id")
            or ""
        )
        workflow_id = str(raw_wf_id).strip()
        original_workflow_id = workflow_id  # Giữ nguyên để guard clause kiểm tra, không bị fallback làm nhiễu

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
            f"⚡ [WEBHOOK] WF: '{workflow_id}' (StorageCfg='{workflow_storage}', DeliveryCfg='{workflow_delivery}') | "
            f"Stage: '{stage_name}' | Job: '{job_id}' | Name: '{task_name}' | Keys: {list(raw_payload.keys())}"
        )

        # 4. Bóc tách Custom Fields & thông tin khách hàng
        job_cf = job_data.get("custom_fields")
        if isinstance(job_cf, str):
            try:
                job_cf = json.loads(job_cf)
            except Exception:
                job_cf = {}

        root_custom_fields = {
            k[7:]: v
            for k, v in payload.items()
            if str(k).startswith("custom_")
            and not str(k).endswith(("-fids", "-direct_links", "-names"))
            and not isinstance(v, (dict, list))
        }

        custom_fields_data = {
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
        content_fallback = clean_base_content(
            payload.get("content_short") or payload.get("note") or job_data.get("content_short") or ""
        )

        combined_parts = [p for p in (raw_addr_field, raw_note_field) if p]
        base_note = "\n".join(combined_parts) if combined_parts else content_fallback

        parsed_info = parse_task_name(task_name, base_note)
        extracted_code, extracted_phone_title = extract_identifier_code_or_phone(
            f"{task_name} {base_note}"
        )
        extracted_phone_norm = (
            normalize_phone(raw_phone_field)
            or normalize_phone(parsed_info.get("phone", "").strip())
            or normalize_phone(extracted_phone_title or "")
            or None
        )

        # 5. Tìm phiếu đã tồn tại trong CSDL
        parent_job_id = str(
            payload.get("parent_id")
            or payload.get("source_job_id")
            or payload.get("origin_job_id")
            or payload.get("source_id")
            or payload.get("origin_id")
            or payload.get("forwarded_from_job_id")
            or payload.get("from_job_id")
            or job_data.get("parent_id")
            or job_data.get("parent_job_id")
            or job_data.get("source_job_id")
            or job_data.get("origin_job_id")
            or scanned_source_job
            or ""
        ).strip()

        related_ids = collect_related_job_ids(payload, job_id, job_data)

        possibly_forwarded = (
            workflow_id in {workflow_storage, workflow_delivery, ""}
            or "[fwd]" in task_name.lower()
            or bool(related_ids)
            or bool(parent_job_id)
            or bool(scanned_source_job)
        )

        final_code_for_match = extracted_code or scanned_ticket_code

        existing_ticket = await find_existing_repair_ticket(
            candidate_job_ids=[job_id, *related_ids, parent_job_id, scanned_source_job or ""],
            code=final_code_for_match,
            phone=extracted_phone_norm,
            allow_phone_match=possibly_forwarded,
        )

        # Chuẩn hóa Workflow ID nếu Base gửi thiếu (chỉ khi đã tìm thấy phiếu)
        if existing_ticket:
            stored_base_job_id = str(existing_ticket.get("base_job_id") or "").strip()
            stored_delivery_job_id = str(existing_ticket.get("delivery_job_id") or "").strip()
            stored_storage_job_id = str(existing_ticket.get("storage_job_id") or "").strip()

            if stored_delivery_job_id and job_id == stored_delivery_job_id:
                workflow_id = workflow_delivery
            elif stored_storage_job_id and job_id == stored_storage_job_id:
                workflow_id = workflow_storage
            elif not workflow_id and job_id != stored_base_job_id:
                curr_proc = str(existing_ticket.get("current_process") or "").strip().lower()
                if curr_proc == "delivery":
                    workflow_id = workflow_delivery
                elif curr_proc == "storage":
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
                    f"Parent='{parent_job_id}' ScannedSrc='{scanned_source_job}' "
                    f"Code='{final_code_for_match}' Phone='{extracted_phone_norm}' "
                    f"Task='{task_name}' PayloadKeys={list(payload.keys())}"
                )
                return {
                    "status": "ignored",
                    "message": "Không tìm thấy phiếu sửa chữa gốc để chuyển tiếp (đã thử JobID/Parent/Mã phiếu/SĐT).",
                    "code": None,
                }

        # 6. Hợp nhất dữ liệu & trích xuất hình ảnh
        now_iso = datetime.now(timezone.utc).isoformat()
        extracted_name = parsed_info.get("customer_name", "").strip()
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

        img_groups, _ = extract_all_base_images(
            payload,
            workflow_id,
            {**settings, "workflow_storage_id": workflow_storage, "workflow_delivery_id": workflow_delivery},
        )
        old_received = (existing_ticket.get("received_images") if existing_ticket else None) or []
        old_repair = (existing_ticket.get("repair_images") if existing_ticket else None) or []
        old_delivery = (existing_ticket.get("delivery_images") if existing_ticket else None) or []
        old_storage = (existing_ticket.get("storage_images") if existing_ticket else None) or []

        # 7. Điều hướng cập nhật CSDL theo từng quy trình
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

        else:
            ticket_code = await _process_repair_webhook(
                payload=payload,
                job_data=job_data,
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
        logger.error(f"[Webhook Error] {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xử lý webhook")
