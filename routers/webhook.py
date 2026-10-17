import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
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

# Cache RAM
_settings_cache: Optional[Dict[str, str]] = None
_settings_updated: float = 0
_status_map_cache: Optional[dict] = None
_status_map_updated: float = 0

# Cooldown đối soát ngầm
_last_reconcile_time = {"repair": 0.0, "storage": 0.0, "delivery": 0.0}
RECONCILE_COOLDOWN = 120.0

# Phiếu mới tạo / gán job_id lần đầu sẽ được tạm khóa đối soát trong N phút
SKIP_RECONCILE_MINUTES = 5


def _skip_reconcile_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=SKIP_RECONCILE_MINUTES)).isoformat()


async def safe_background_reconcile(app_name: str):
    global _last_reconcile_time
    current_time = time.time()
    if current_time - _last_reconcile_time.get(app_name, 0) < RECONCILE_COOLDOWN:
        return
    _last_reconcile_time[app_name] = current_time
    try:
        from routers.sync import reconcile_tickets
        logger.info(f"🔄 [AUTO-SYNC] Kích hoạt đối soát ngầm cho quy trình: {app_name}")
        await reconcile_tickets(app_name=app_name)
    except Exception as e:
        logger.error(f"Lỗi khi chạy đối soát ngầm cho {app_name}: {e}")


# ---------- HELPER: cấu hình động ----------
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
            lambda: supabase.table("system_settings").select("key, value").execute()
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
            lambda: supabase.table("status_mappings").select("base_stage, system_status").execute()
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


async def find_existing_repair_ticket(job_id: str, code: Optional[str], phone: Optional[str]):
    def _query():
        res = supabase.table("repairs").select("*").or_(
            f"base_job_id.eq.{job_id},storage_job_id.eq.{job_id},delivery_job_id.eq.{job_id}"
        ).limit(1).execute()
        if res.data:
            return res.data[0]
        if code:
            res_code = supabase.table("repairs").select("*").eq("code", code).limit(1).execute()
            if res_code.data:
                return res_code.data[0]
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


# ---------- WEBHOOK CHÍNH ----------
@router.post("/base")
async def receive_base_webhook(request: Request, bg_tasks: BackgroundTasks):
    try:
        settings = await get_system_settings()
        workflow_repair = settings.get("workflow_repair_id") or settings.get("workflow_repair", "20025")
        workflow_storage = settings.get("workflow_storage_id") or settings.get("workflow_storage", "20036")
        workflow_delivery = settings.get("workflow_delivery_id") or settings.get("workflow_delivery", "20037")
        configured_secret = settings.get("base_shared_secret", "")

        # 1. Parse body
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
        else:
            form_data = await request.form()
            payload = dict(form_data)

        if "custom_fields" in payload and isinstance(payload["custom_fields"], str):
            try:
                payload["custom_fields"] = json.loads(payload["custom_fields"])
            except Exception:
                pass

        job_data = payload.get("job") if isinstance(payload.get("job"), dict) else {}

        # 2. Xác thực secret
        incoming_secret = (
            request.headers.get("x-base-secret")
            or request.query_params.get("secret")
            or payload.get("secret")
        )
        if configured_secret and incoming_secret != configured_secret:
            logger.warning("Webhook: Secret Token không khớp hoặc bị thiếu")
            raise HTTPException(status_code=401, detail="Xác thực không hợp lệ")

        # 3. Job ID + Task Name
        job_id = str(payload.get("job_id") or payload.get("id") or job_data.get("id") or "").strip()
        if not job_id or job_id.lower() == "none":
            return {"status": "ignored", "message": "Thiếu job_id hợp lệ", "code": None}

        task_name = str(
            payload.get("task_name") or payload.get("name") or payload.get("job_name")
            or job_data.get("name") or job_data.get("title") or ""
        ).strip()

        state = str(payload.get("state") or job_data.get("state") or "").strip().lower()

        # Workflow ID
        workflow_obj = payload.get("workflow") or job_data.get("workflow") or {}
        if isinstance(workflow_obj, str):
            try:
                workflow_obj = json.loads(workflow_obj)
            except Exception:
                workflow_obj = {}
        raw_wf_id = (
            payload.get("workflow_id") or job_data.get("workflow_id") or job_data.get("wf_id")
            or (workflow_obj.get("id") if isinstance(workflow_obj, dict) else None)
            or payload.get("source_container_id") or ""
        )
        workflow_id = str(raw_wf_id).strip()

        # Stage Info
        stage_obj = payload.get("stage") or job_data.get("stage") or {}
        if isinstance(stage_obj, str):
            try:
                stage_obj = json.loads(stage_obj)
            except Exception:
                stage_obj = {}
        if isinstance(stage_obj, dict):
            stage_name = str(stage_obj.get("name") or "Tiếp nhận").strip()
            metatype = str(stage_obj.get("metatype") or "").strip().lower()
        else:
            stage_name = str(payload.get("stage_name") or stage_obj or "Tiếp nhận").strip()
            metatype = ""

        logger.info(f"⚡ [WEBHOOK] WF: '{workflow_id}' | Stage: '{stage_name}' | Job: '{job_id}'")

        # 4. Tìm phiếu cũ
        extracted_code, extracted_phone = extract_identifier_code_or_phone(task_name)
        existing_ticket = await find_existing_repair_ticket(job_id, extracted_code, extracted_phone)

        if existing_ticket:
            curr_proc = str(existing_ticket.get("current_process") or "").strip().lower()
            stored_delivery = str(existing_ticket.get("delivery_job_id") or "").strip()
            stored_storage = str(existing_ticket.get("storage_job_id") or "").strip()
            if job_id == stored_delivery or curr_proc == "delivery":
                workflow_id = workflow_delivery
            elif job_id == stored_storage or curr_proc == "storage":
                workflow_id = workflow_storage

        if not workflow_id:
            workflow_id = workflow_repair

        # Chặn tạo đơn mới từ luồng storage/delivery
        if not existing_ticket and str(workflow_id) not in [str(workflow_repair), ""]:
            logger.warning(
                f"⛔ [BLOCKED] Từ chối tạo đơn mới nhảy bước! WF: {workflow_id} "
                f"(kỳ vọng: {workflow_repair}) | Job: {job_id}"
            )
            return {"status": "ignored", "message": "Không thể tạo mới đơn từ Lưu kho/Giao nhận.", "code": None}

        now_iso = datetime.now(timezone.utc).isoformat()
        ticket_code = existing_ticket.get("code") if existing_ticket else None

        # 5. Bóc tách nội dung
        raw_custom_note = (
            payload.get("custom_dia_chi") or payload.get("custom_ghi_chu")
            or payload.get("description") or job_data.get("description")
        )
        if raw_custom_note and str(raw_custom_note).strip().lower() != "no value":
            base_note = str(raw_custom_note).strip()
        else:
            base_note = clean_base_content(
                payload.get("content_short") or payload.get("note") or job_data.get("content_short") or ""
            )

        parsed_info = parse_task_name(task_name, base_note)
        extracted_name = parsed_info.get("customer_name", "").strip()
        extracted_phone_norm = normalize_phone(
            parsed_info.get("phone", "").strip() or (extracted_phone or "")
        ) or None
        extracted_addr = parsed_info.get("address", "").strip()
        extracted_req_note = parsed_info.get("note", "").strip()
        extracted_rec_note = parsed_info.get("received_note", "").strip()

        if existing_ticket:
            final_customer_name = extracted_name if (extracted_name and extracted_name != "Khách lẻ") else existing_ticket.get("customer_name")
            final_phone = extracted_phone_norm if extracted_phone_norm else existing_ticket.get("phone")
            final_address = extracted_addr if extracted_addr else existing_ticket.get("address")
            final_note = extracted_req_note if (extracted_req_note and extracted_req_note != "Chưa có yêu cầu") else existing_ticket.get("note")
            final_received_note = extracted_rec_note if extracted_rec_note else existing_ticket.get("received_note")
        else:
            final_customer_name = extracted_name or "Khách lẻ"
            final_phone = extracted_phone_norm
            final_address = extracted_addr
            final_note = extracted_req_note or "Chưa có yêu cầu"
            final_received_note = extracted_rec_note

        # 6. Ảnh
        img_groups, _ = extract_all_base_images(payload, workflow_id, settings)
        old_received = (existing_ticket.get("received_images") if existing_ticket else None) or []
        old_repair = (existing_ticket.get("repair_images") if existing_ticket else None) or []
        old_delivery = (existing_ticket.get("delivery_images") if existing_ticket else None) or []
        old_storage = (existing_ticket.get("storage_images") if existing_ticket else None) or []

        # =========================================================
        # 6A. LƯU KHO
        # =========================================================
        if str(workflow_id) == str(workflow_storage):
            stage_lower = stage_name.lower().strip()
            if any(kw in stage_lower for kw in ["tiêu hủy", "đã tiêu hủy", "destroy"]):
                storage_status = "destroyed"
            elif (metatype in {"done", "finished", "success"} or state in {"done", "finished", "success"}
                  or any(kw in stage_lower for kw in ["gửi trả", "đã trả", "đã gửi", "done", "hoàn thành", "hoàn tất"])):
                storage_status = "dispatched"
            else:
                storage_status = "in_company"

            update_payload = {
                "storage_job_id": job_id,
                "storage_status": storage_status,
                "current_process": "storage",
                "customer_name": final_customer_name,
                "phone": final_phone,
                "address": final_address,
                "note": final_note,
                "updated_at": now_iso,
            }
            # 🛡️ Nếu đây là lần đầu gán storage_job_id -> tạm khóa đối soát
            old_storage_job = str(existing_ticket.get("storage_job_id") or "").strip() if existing_ticket else ""
            if old_storage_job != job_id:
                update_payload["skip_reconcile_until"] = _skip_reconcile_iso()

            new_storage_imgs = get_new_images(img_groups.get("storage", []), old_received, old_repair, old_delivery, old_storage)
            if new_storage_imgs:
                update_payload["storage_images"] = old_storage + new_storage_imgs
            if storage_status == "dispatched":
                update_payload["status"] = "returned"
                update_payload["returned_date"] = now_iso

            await asyncio.to_thread(
                lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
            )
            logger.info(f"📦 [LƯU KHO] Cập nhật phiếu {ticket_code} -> storage_status: {storage_status}")

        # =========================================================
        # 6B. GIAO NHẬN
        # =========================================================
        elif str(workflow_id) == str(workflow_delivery):
            stage_lower = stage_name.lower().strip()
            if any(kw in stage_lower for kw in ["thất bại", "hủy giao", "không thành công", "fail", "cancel"]):
                delivery_status = "failed"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "failed",
                    "status": "cancelled",
                    "failure_reason": "Giao nhận thất bại",
                    "current_process": "delivery",
                    "customer_name": final_customer_name,
                    "phone": final_phone,
                    "address": final_address,
                    "note": final_note,
                    "updated_at": now_iso,
                }
            elif (any(kw in stage_lower for kw in ["hoàn thành", "hoàn tất", "đã giao", "xong", "success", "done", "finished"])
                  or metatype in {"done", "finished", "success"} or state in {"done", "finished", "success"}):
                delivery_status = "delivered"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "delivered",
                    "status": "returned",
                    "returned_date": now_iso,
                    "current_process": "delivery",
                    "customer_name": final_customer_name,
                    "phone": final_phone,
                    "address": final_address,
                    "note": final_note,
                    "updated_at": now_iso,
                }
            elif any(kw in stage_lower for kw in ["xử lý", "đang giao", "đi giao", "vận chuyển", "delivering", "shipping", "processing"]):
                delivery_status = "delivering"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "delivering",
                    "current_process": "delivery",
                    "customer_name": final_customer_name,
                    "phone": final_phone,
                    "address": final_address,
                    "note": final_note,
                    "updated_at": now_iso,
                }
            else:
                delivery_status = "pending"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "pending",
                    "current_process": "delivery",
                    "customer_name": final_customer_name,
                    "phone": final_phone,
                    "address": final_address,
                    "note": final_note,
                    "updated_at": now_iso,
                }

            # 🛡️ Lần đầu gán delivery_job_id -> tạm khóa đối soát
            old_delivery_job = str(existing_ticket.get("delivery_job_id") or "").strip() if existing_ticket else ""
            if old_delivery_job != job_id:
                update_payload["skip_reconcile_until"] = _skip_reconcile_iso()

            new_delivery_imgs = get_new_images(img_groups.get("delivery", []), old_received, old_repair, old_delivery)
            if new_delivery_imgs:
                update_payload["delivery_images"] = old_delivery + new_delivery_imgs

            await asyncio.to_thread(
                lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
            )
            logger.info(f"🚚 [GIAO NHẬN] Cập nhật phiếu {ticket_code} -> delivery_status: {delivery_status}")

        # =========================================================
        # 6C. SỬA CHỮA (mặc định)
        # =========================================================
        else:
            EMPTY_VALUES = {"", "no value", "null", "none", "n/a", "na", "-", "0", "[]", "{}",
                            "không có", "không có giá trị", "chưa có"}

            def clean_val(v) -> str:
                s = str(v or "").strip()
                return "" if s.lower() in EMPTY_VALUES else s

            base_failed_reason = clean_val(
                payload.get("failed_reason") or payload.get("fail_reason") or job_data.get("failed_reason")
            )
            base_failed_note = clean_val(
                payload.get("failed_note") or payload.get("fail_note") or job_data.get("failed_note")
            )

            custom_tinh_trang = payload.get("custom_tinh_trang_thiet_bi") or ""
            custom_tinh_trang = "" if str(custom_tinh_trang).strip().lower() == "no value" else str(custom_tinh_trang).strip()

            status_map = await get_dynamic_status_map()
            mapped_status = validate_status(stage_name, status_map, metatype=metatype, state=state) or "received"

            stage_lower = stage_name.lower()
            is_fail_stage = (
                metatype in {"fail", "failed", "cancel", "cancelled"}
                or state in {"fail", "failed", "cancel", "cancelled"}
                or any(kw in stage_lower for kw in ["hủy", "thất bại", "không sửa được", "fail", "cancel"])
            )

            if base_failed_reason or base_failed_note:
                mapped_status = "cancelled"

            # 🛡️ Quan trọng: với phiếu MỚI, nếu không phải stage hủy thật sự thì ép về received
            if not existing_ticket and mapped_status == "cancelled" and not is_fail_stage:
                mapped_status = "received"

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

            repair_imgs = img_groups.get("repair", [])
            if mapped_status == "received":
                new_received_imgs = get_new_images(repair_imgs, old_received)
                if new_received_imgs:
                    update_payload["received_images"] = old_received + new_received_imgs
            else:
                new_repair_imgs = get_new_images(repair_imgs, old_received, old_repair)
                if new_repair_imgs:
                    update_payload["repair_images"] = old_repair + new_repair_imgs

            if mapped_status == "returned":
                update_payload["returned_date"] = now_iso

            if mapped_status == "cancelled" or base_failed_reason or base_failed_note:
                update_payload["is_fixed"] = False
                update_payload["repair_result"] = "failed"
                cancel_parts = []
                if base_failed_reason:
                    cancel_parts.append(f"Lý do: {base_failed_reason}")
                if base_failed_note:
                    cancel_parts.append(f"Ghi chú: {base_failed_note}")
                cancel_str = " - ".join(cancel_parts) if cancel_parts else "KTV báo hủy sửa"
                update_payload["failure_reason"] = cancel_str

                curr_received = (existing_ticket.get("received_note") or "").strip() if existing_ticket else final_received_note
                if cancel_str not in curr_received:
                    update_payload["received_note"] = f"{curr_received} [{cancel_str}]" if curr_received else f"[{cancel_str}]"

            if existing_ticket:
                if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
                    ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
                    curr_note = final_note or (existing_ticket.get("note") or "").strip()
                    if ktv_text not in curr_note:
                        update_payload["note"] = f"{curr_note} | {ktv_text}" if curr_note else ktv_text

                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                ticket_code = existing_ticket["code"]
                logger.info(f"🛠️ [SỬA CHỮA] Cập nhật phiếu {ticket_code} thành công (status={mapped_status})")
            else:
                if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
                    ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
                    final_note = f"{final_note} | {ktv_text}" if final_note else ktv_text

                ticket_code = extracted_code or generate_ticket_code()
                new_record = {
                    "base_job_id": job_id,
                    "code": ticket_code,
                    "customer_name": final_customer_name,
                    "phone": final_phone,
                    "address": final_address,
                    "note": final_note,
                    "received_note": update_payload.get("received_note", final_received_note),
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
                    # 🛡️ Tạm khóa đối soát 5 phút cho phiếu mới tạo
                    "skip_reconcile_until": _skip_reconcile_iso(),
                }
                try:
                    await asyncio.to_thread(
                        lambda: supabase.table("repairs").insert(new_record).execute()
                    )
                    logger.info(f"🛠️ [SỬA CHỮA] Tạo phiếu mới thành công: {ticket_code} (status={mapped_status})")
                except Exception as insert_err:
                    if "duplicate" in str(insert_err).lower():
                        logger.info(f"Webhook: Trùng job_id {job_id} -> cập nhật lại")
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

        # Background đối soát
        current_app_name = "repair"
        if str(workflow_id) == str(workflow_storage):
            current_app_name = "storage"
        elif str(workflow_id) == str(workflow_delivery):
            current_app_name = "delivery"

        bg_tasks.add_task(safe_background_reconcile, current_app_name)

        return {
            "status": "success",
            "message": "Đồng bộ webhook thành công (Đã kích hoạt quét rác ngầm)",
            "workflow_id": workflow_id,
            "code": ticket_code,
            "stage_name": stage_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Webhook Error] {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xử lý webhook")