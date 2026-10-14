import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict

import httpx
from fastapi import APIRouter, HTTPException, Request

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

# Cache RAM cho cấu hình và map trạng thái
_settings_cache: Optional[Dict[str, str]] = None
_settings_updated: float = 0

_status_map_cache: Optional[dict] = None
_status_map_updated: float = 0


# =========================================================
# HELPER FUNCTIONS - CẤU HÌNH ĐỘNG TỪ DB
# =========================================================
async def get_system_settings() -> Dict[str, str]:
    """Tải toàn bộ cấu hình từ system_settings (có caching)"""
    global _settings_cache, _settings_updated
    now = datetime.now(timezone.utc).timestamp()
    
    ttl = float(_settings_cache.get("cache_ttl_seconds", 60)) if _settings_cache else 60.0

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
    """Đọc ánh xạ trạng thái từ DB (có Caching)"""
    global _status_map_cache, _status_map_updated
    now = datetime.now(timezone.utc).timestamp()

    settings = await get_system_settings()
    ttl = float(settings.get("cache_ttl_seconds", 60))

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


async def find_existing_repair_ticket(job_id: str, code: Optional[str], phone: Optional[str]):
    """Tìm phiếu đã tồn tại trong DB theo base_job_id, storage_job_id, delivery_job_id, Mã phiếu hoặc SĐT"""
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

        if phone:
            res_phone = supabase.table("repairs").select("*").eq("phone", phone).order("created_at", desc=True).limit(1).execute()
            if res_phone.data:
                return res_phone.data[0]

        return None

    return await asyncio.to_thread(_query)


def get_new_images(incoming_imgs: list, *old_img_arrays) -> list:
    """Helper: Lọc ảnh trùng lặp. Lấy mảng ảnh mới trừ đi các mảng ảnh cũ."""
    old_set = set()
    for arr in old_img_arrays:
        if arr:
            old_set.update(arr)
    # Giữ nguyên thứ tự của ảnh mới, chỉ lấy những ảnh chưa từng có
    return [img for img in incoming_imgs if img not in old_set]


# =========================================================
# WEBHOOK RECEIVER ROUTE
# =========================================================
@router.post("/base")
async def receive_base_webhook(request: Request):
    try:
        # Lấy cấu hình từ DB
        settings = await get_system_settings()
        workflow_repair = settings.get("workflow_repair_id", "20025")
        workflow_storage = settings.get("workflow_storage_id", "20036")
        workflow_delivery = settings.get("workflow_delivery_id", "20037")
        configured_secret = settings.get("base_shared_secret", "")

        # 1. Parse Request Body
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

        # 2. Xác thực Secret Token
        incoming_secret = (
            request.headers.get("x-base-secret")
            or request.query_params.get("secret")
            or payload.get("secret")
        )
        if configured_secret:
            if not incoming_secret or incoming_secret != configured_secret:
                logger.warning("Webhook: Secret Token không khớp hoặc bị thiếu")
                raise HTTPException(status_code=401, detail="Xác thực không hợp lệ")

        # 3. Trích xuất thông tin Job ID & Task Name
        job_id = str(payload.get("job_id") or payload.get("id", "")).strip()
        if not job_id or job_id.lower() == "none":
            return {"status": "ignored", "message": "Thiếu job_id hợp lệ", "code": None}
        task_name = str(payload.get("task_name") or payload.get("name") or payload.get("job_name") or "").strip()
        state = str(payload.get("state") or "").strip().lower()

        is_archive_val = str(payload.get("is_archive") or "0").strip()
        is_job_archived_or_deleted = (
            is_archive_val in ["1", "true", "True"]
            or state in ["archived", "deleted", "deleted_job", "cancelled"]
        )

        workflow_obj = payload.get("workflow") or {}
        if isinstance(workflow_obj, str):
            try:
                workflow_obj = json.loads(workflow_obj)
            except Exception:
                workflow_obj = {}
        raw_wf_id = (
            payload.get("workflow_id")
            or workflow_obj.get("id")
            or payload.get("source_container_id")
            or ""
        )
        workflow_id = str(raw_wf_id).strip()

        stage_obj = payload.get("stage") or {}
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
        
        logger.info(f"⚡ [WEBHOOK] WF: {workflow_id} | Stage: '{stage_name}' | Job: {job_id}")

        # Tìm phiếu trong DB
        extracted_code, extracted_phone = extract_identifier_code_or_phone(task_name)
        existing_ticket = await find_existing_repair_ticket(job_id, extracted_code, extracted_phone)

        # Bảo vệ luồng Workflow ID
        if existing_ticket:
            curr_proc = str(existing_ticket.get("current_process") or "").strip().lower()
            stored_delivery_job_id = str(existing_ticket.get("delivery_job_id") or "").strip()
            stored_storage_job_id = str(existing_ticket.get("storage_job_id") or "").strip()

            if job_id == stored_delivery_job_id or curr_proc == "delivery":
                if workflow_id not in {workflow_delivery, workflow_storage}:
                    workflow_id = workflow_delivery
            elif job_id == stored_storage_job_id or curr_proc == "storage":
                if workflow_id not in {workflow_delivery, workflow_storage}:
                    workflow_id = workflow_storage
            elif not workflow_id:
                if curr_proc == "delivery":
                    workflow_id = workflow_delivery
                elif curr_proc == "storage":
                    workflow_id = workflow_storage
                else:
                    workflow_id = workflow_repair
        if not workflow_id:
            workflow_id = workflow_repair

        now_iso = datetime.now(timezone.utc).isoformat()
        ticket_code = existing_ticket.get("code") if existing_ticket else None

        # Xử lý Hủy / Lưu trữ
        if is_job_archived_or_deleted:
            if existing_ticket:
                cancel_reason_str = "Công việc đã bị lưu trữ / xóa trên Base"
                curr_received = (existing_ticket.get("received_note") or "").strip()
                new_received = f"{curr_received} [{cancel_reason_str}]" if (curr_received and cancel_reason_str not in curr_received) else (curr_received or f"[{cancel_reason_str}]")
                update_payload = {
                    "status": "cancelled",
                    "is_fixed": False,
                    "repair_result": "failed",
                    "failure_reason": cancel_reason_str,
                    "received_note": new_received,
                    "updated_at": now_iso,
                }
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                logger.info(f"🗑️ [BASE ARCHIVED] Đã chuyển phiếu {ticket_code} sang HỦY")
                return {"status": "success", "message": "Đã ghi nhận công việc bị xóa", "code": ticket_code}
            else:
                return {"status": "ignored", "message": "Công việc bị xóa và không có trong DB"}

        # 4. TRÍCH XUẤT HÌNH ẢNH (Truyền thêm WF ID & Settings)
        img_groups, images_by_field = extract_all_base_images(payload, workflow_id, settings)
        
        # Chuẩn bị dữ liệu ảnh cũ từ DB để so sánh (Deduping)
        old_received = existing_ticket.get("received_images") or [] if existing_ticket else []
        old_repair = existing_ticket.get("repair_images") or [] if existing_ticket else []
        old_delivery = existing_ticket.get("delivery_images") or [] if existing_ticket else []
        old_storage = existing_ticket.get("storage_images") or [] if existing_ticket else []

        # =========================================================
        # 5A. QUY TRÌNH LƯU KHO
        # =========================================================
        if workflow_id == workflow_storage:
            stage_lower = stage_name.lower().strip()
            if any(kw in stage_lower for kw in ["tiêu hủy", "đã tiêu hủy", "destroy"]):
                storage_status = "destroyed"
            elif metatype in {"done", "finished", "success"} or state in {"done", "finished", "success"} or any(kw in stage_lower for kw in ["gửi trả", "đã trả", "đã gửi", "done", "hoàn thành", "hoàn tất"]):
                storage_status = "dispatched"
            else:
                storage_status = "in_company"

            update_payload = {
                "storage_job_id": job_id,
                "storage_status": storage_status,
                "current_process": "storage",
                "updated_at": now_iso,
            }

            # Lọc ảnh kho mới tinh (Trừ đi ảnh cũ các bước trước)
            new_storage_imgs = get_new_images(img_groups["storage"], old_received, old_repair, old_delivery, old_storage)
            if new_storage_imgs:
                update_payload["storage_images"] = old_storage + new_storage_imgs

            if storage_status == "dispatched":
                update_payload["status"] = "returned"
                update_payload["returned_date"] = now_iso

            if existing_ticket:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                logger.info(f"📦 [LƯU KHO] Cập nhật phiếu {ticket_code} -> status: {storage_status}")
            else:
                return {"status": "ignored", "message": "Không tìm thấy phiếu để cập nhật kho"}

        # =========================================================
        # 5B. QUY TRÌNH GIAO NHẬN
        # =========================================================
        elif workflow_id == workflow_delivery:
            stage_lower = stage_name.lower().strip()
            if any(kw in stage_lower for kw in ["thất bại", "hủy giao", "không thành công", "fail", "cancel"]):
                delivery_status = "failed"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "failed",
                    "status": "cancelled",
                    "failure_reason": "Giao nhận thất bại",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }
            elif any(kw in stage_lower for kw in ["hoàn thành", "hoàn tất", "đã giao", "xong", "success", "done", "finished"]) or metatype in {"done", "finished", "success"} or state in {"done", "finished", "success"}:
                delivery_status = "delivered"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "delivered",
                    "status": "returned",
                    "returned_date": now_iso,
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }
            elif any(kw in stage_lower for kw in ["xử lý", "đang giao", "đi giao", "vận chuyển", "delivering", "shipping", "processing"]):
                delivery_status = "delivering"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "delivering",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }
            else:
                delivery_status = "pending"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "pending",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }

            # Lọc ảnh giao nhận mới tinh (Trừ đi ảnh cũ)
            new_delivery_imgs = get_new_images(img_groups["delivery"], old_received, old_repair, old_delivery)
            if new_delivery_imgs:
                update_payload["delivery_images"] = old_delivery + new_delivery_imgs

            if existing_ticket:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                logger.info(f"🚚 [GIAO NHẬN] Cập nhật phiếu {ticket_code} -> status: {delivery_status}")
            else:
                return {"status": "ignored", "message": "Không tìm thấy phiếu để cập nhật giao nhận"}

        # =========================================================
        # 5C. QUY TRÌNH SỬA CHỮA MẶC ĐỊNH
        # =========================================================
        else:
            job_data = payload.get("job") if isinstance(payload.get("job"), dict) else {}
            base_failed_reason = (payload.get("failed_reason") or payload.get("fail_reason") or job_data.get("failed_reason") or "").strip()
            base_failed_note = (payload.get("failed_note") or payload.get("fail_note") or job_data.get("failed_note") or "").strip()
            if base_failed_reason.lower() == "no value": base_failed_reason = ""
            if base_failed_note.lower() == "no value": base_failed_note = ""

            raw_custom_note = payload.get("custom_dia_chi") or payload.get("custom_ghi_chu") or payload.get("description")
            if raw_custom_note and str(raw_custom_note).strip().lower() != "no value":
                base_note = str(raw_custom_note).strip()
            else:
                base_note = clean_base_content(payload.get("content_short") or payload.get("note") or "")

            custom_tinh_trang = payload.get("custom_tinh_trang_thiet_bi") or ""
            custom_tinh_trang = "" if str(custom_tinh_trang).strip().lower() == "no value" else str(custom_tinh_trang).strip()

            status_map = await get_dynamic_status_map()
            mapped_status = validate_status(stage_name, status_map, metatype=metatype, state=state)
            if base_failed_reason or base_failed_note:
                mapped_status = "cancelled"

            parsed_info = parse_task_name(task_name, base_note)
            customer_name = parsed_info.get("customer_name", "Khách lẻ").strip() or "Khách lẻ"
            phone = normalize_phone(parsed_info.get("phone", "").strip() or (extracted_phone or "")) or None
            address = parsed_info.get("address", "").strip()
            note = parsed_info.get("note", "Chưa có yêu cầu").strip() or "Chưa có yêu cầu"
            received_note = parsed_info.get("received_note", "").strip()

            update_payload = {
                "status": mapped_status,
                "current_process": "repair",
                "updated_at": now_iso,
            }

            # ================= LỌC ẢNH SỬA CHỮA =================
            if mapped_status == "received":
                # Nếu là Tiếp nhận -> Toàn bộ ảnh rổ "repair" là ảnh tiếp nhận
                new_received_imgs = get_new_images(img_groups["repair"], old_received)
                if new_received_imgs:
                    update_payload["received_images"] = old_received + new_received_imgs
            else:
                # Nếu chuyển sang Đang sửa/Hoàn thành -> Bỏ qua ảnh cũ của Bước tiếp nhận
                new_repair_imgs = get_new_images(img_groups["repair"], old_received, old_repair)
                if new_repair_imgs:
                    update_payload["repair_images"] = old_repair + new_repair_imgs
            # ====================================================

            if mapped_status == "returned":
                update_payload["returned_date"] = now_iso

            if mapped_status == "cancelled" or base_failed_reason or base_failed_note:
                update_payload["is_fixed"] = False
                update_payload["repair_result"] = "failed"
                cancel_parts = []
                if base_failed_reason: cancel_parts.append(f"Lý do: {base_failed_reason}")
                if base_failed_note: cancel_parts.append(f"Ghi chú: {base_failed_note}")
                cancel_str = " - ".join(cancel_parts) if cancel_parts else "KTV báo hủy sửa"
                update_payload["failure_reason"] = cancel_str
                
                curr_received = (existing_ticket.get("received_note") or "").strip() if existing_ticket else received_note
                if cancel_str not in curr_received:
                    update_payload["received_note"] = f"{curr_received} [{cancel_str}]" if curr_received else f"[{cancel_str}]"

            if existing_ticket:
                if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
                    ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
                    curr_note = (existing_ticket.get("note") or "").strip()
                    if ktv_text not in curr_note:
                        update_payload["note"] = f"{curr_note} | {ktv_text}" if curr_note else ktv_text

                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                ticket_code = existing_ticket["code"]
                logger.info(f"🛠️ [SỬA CHỮA] Cập nhật phiếu {ticket_code} -> status: {mapped_status}")
            else:
                if mapped_status in {"fixed", "returned"} and custom_tinh_trang:
                    ktv_text = f"Tình trạng KTV báo: {custom_tinh_trang}"
                    note = f"{note} | {ktv_text}" if note else ktv_text

                ticket_code = extracted_code or generate_ticket_code()
                new_record = {
                    "base_job_id": job_id,
                    "code": ticket_code,
                    "customer_name": customer_name,
                    "phone": phone,
                    "address": address,
                    "note": note,
                    "received_note": update_payload.get("received_note", received_note),
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
                    logger.info(f"🛠️ [SỬA CHỮA] Tạo phiếu mới {ticket_code}")
                except Exception as insert_err:
                    if "duplicate" in str(insert_err).lower():
                        logger.info(f"Webhook: Trùng job_id {job_id} -> Đang thử cập nhật lại")
                        ex = await asyncio.to_thread(lambda: supabase.table("repairs").select("id, code").eq("base_job_id", job_id).limit(1).execute())
                        if ex.data:
                            await asyncio.to_thread(lambda: supabase.table("repairs").update(update_payload).eq("id", ex.data[0]["id"]).execute())
                            ticket_code = ex.data[0]["code"]
                    else:
                        raise

        return {
            "status": "success",
            "message": "Đồng bộ webhook thành công",
            "workflow_id": workflow_id,
            "code": ticket_code,
            "stage_name": stage_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Webhook Error] {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xử lý webhook")