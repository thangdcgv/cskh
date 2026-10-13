import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

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
from websocket_manager import manager

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/webhook", tags=["Webhook"])

# =========================================================
# WORKFLOW IDS & CONSTANTS
# =========================================================
WORKFLOW_REPAIR = "20025"    # Quy trình Sửa chữa
WORKFLOW_STORAGE = "20036"   # Quy trình Lưu kho
WORKFLOW_DELIVERY = "20037"  # Quy trình Giao nhận

ALLOWED_STATUSES = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}

_status_map_cache: Optional[dict] = None
_status_map_updated: float = 0

_secret_cache: Optional[str] = None
_secret_updated: float = 0

_CACHE_TTL = 60  # Giây


# =========================================================
# HELPER FUNCTIONS
# =========================================================
async def get_system_secret() -> str:
    """Lấy Shared Secret Token từ DB (có Caching 60s)"""
    global _secret_cache, _secret_updated
    now = datetime.now(timezone.utc).timestamp()

    if _secret_cache is not None and (now - _secret_updated) < _CACHE_TTL:
        return _secret_cache

    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings")
            .select("value")
            .eq("key", "base_shared_secret")
            .limit(1)
            .execute()
        )
        if res.data:
            _secret_cache = str(res.data[0].get("value", "")).strip()
        else:
            _secret_cache = ""
        _secret_updated = now
        return _secret_cache
    except Exception as e:
        logger.warning(f"Không thể lấy base_shared_secret từ DB: {e}")
        return _secret_cache or ""


async def get_dynamic_status_map() -> dict:
    """Đọc ánh xạ trạng thái từ DB (có Caching 60s)"""
    global _status_map_cache, _status_map_updated
    now = datetime.now(timezone.utc).timestamp()

    if _status_map_cache is not None and (now - _status_map_updated) < _CACHE_TTL:
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
        "thông tin đơn": "received",
        "tiếp nhận": "received",
        "1. tiếp nhận": "received",
        "tiếp nhận máy": "received",
        "tiến hành kiểm tra & sửa chữa": "processing",
        "đang sửa": "processing",
        "sửa chữa": "processing",
        "done": "fixed",
        "hoàn thành": "fixed",
        "hoàn tất": "fixed",
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


# =========================================================
# WEBHOOK RECEIVER ROUTE - ĐÃ CẬP NHẬT LOGIC ẢNH
# =========================================================
@router.post("/base")
async def receive_base_webhook(request: Request):
    try:
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
        configured_secret = await get_system_secret()
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

        # Bắt cờ Lưu Trữ / Xóa từ Base
        is_archive_val = str(payload.get("is_archive") or "0").strip()
        is_job_archived_or_deleted = (
            is_archive_val in ["1", "true", "True"]
            or state in ["archived", "deleted", "deleted_job", "cancelled"]
        )

        # Nhận diện Workflow ID an toàn
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

        # Trích xuất tên Stage & Metatype
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
        logger.info(f"⚡ [WEBHOOK BASE] Workflow ID: {workflow_id} | Stage: '{stage_name}' | Metatype: '{metatype}' | Job ID: {job_id} | Name: '{task_name}'")

        # Bóc tách mã phiếu hoặc SĐT để tìm đúng phiếu trong Database
        extracted_code, extracted_phone = extract_identifier_code_or_phone(task_name)
        existing_ticket = await find_existing_repair_ticket(job_id, extracted_code, extracted_phone)

        # Bảo vệ luồng thông minh
        if existing_ticket:
            curr_proc = str(existing_ticket.get("current_process") or "").strip().lower()
            stored_delivery_job_id = str(existing_ticket.get("delivery_job_id") or "").strip()
            stored_storage_job_id = str(existing_ticket.get("storage_job_id") or "").strip()

            if job_id == stored_delivery_job_id or curr_proc == "delivery":
                if workflow_id not in {WORKFLOW_DELIVERY, WORKFLOW_STORAGE}:
                    workflow_id = WORKFLOW_DELIVERY
            elif job_id == stored_storage_job_id or curr_proc == "storage":
                if workflow_id not in {WORKFLOW_DELIVERY, WORKFLOW_STORAGE}:
                    workflow_id = WORKFLOW_STORAGE
            elif not workflow_id:
                if curr_proc == "delivery":
                    workflow_id = WORKFLOW_DELIVERY
                elif curr_proc == "storage":
                    workflow_id = WORKFLOW_STORAGE
                else:
                    workflow_id = WORKFLOW_REPAIR
        if not workflow_id:
            workflow_id = WORKFLOW_REPAIR

        now_iso = datetime.now(timezone.utc).isoformat()
        ticket_code = existing_ticket.get("code") if existing_ticket else None

        # Xử lý khi Job bị lưu trữ / xóa trên Base
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
                logger.info(f"🗑️ [BASE ARCHIVED/DELETED] Đã chuyển phiếu {ticket_code} sang HỦY")
                try:
                    await manager.broadcast({
                        "type": "base_sync",
                        "workflow_id": workflow_id,
                        "job_id": job_id,
                        "code": ticket_code,
                        "stage_name": "Đã lưu trữ / Xóa trên Base",
                        "status": "cancelled"
                    })
                except Exception as ws_err:
                    logger.warning(f"Webhook Broadcast lỗi: {ws_err}")
                return {"status": "success", "message": "Đã ghi nhận công việc bị xóa/lưu trữ", "code": ticket_code, "status_new": "cancelled"}
            else:
                return {"status": "ignored", "message": "Công việc trên Base bị xóa/lưu trữ và không có trong DB"}

        # ✅ TRÍCH XUẤT & PHÂN LOẠI ẢNH THEO GIAI ĐOẠN (KHÔNG GỘP CHÉO)
        img_groups, images_by_field = extract_all_base_images(payload)
        received_imgs = img_groups["received"]
        repair_imgs = img_groups["repair"]
        delivery_imgs = img_groups["delivery"]
        storage_imgs = img_groups["storage"]

        # =========================================================
        # 4A. QUY TRÌNH LƯU KHO (WORKFLOW_ID = 20036)
        # =========================================================
        if workflow_id == WORKFLOW_STORAGE:
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

            # ✅ Chỉ cập nhật KHI CÓ ẢNH thuộc nhóm lưu kho — KHÔNG GHI ĐÈ dữ liệu cũ
            if storage_imgs:
                update_payload["storage_images"] = storage_imgs

            if storage_status == "dispatched":
                update_payload["status"] = "returned"
                update_payload["returned_date"] = now_iso

            if existing_ticket:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                logger.info(f"📦 [LƯU KHO] Cập nhật phiếu {ticket_code} -> storage_status: {storage_status}")
            else:
                logger.warning(f"⚠️ [LƯU KHO] Không tìm thấy phiếu gốc tương ứng cho Job {job_id} ('{task_name}')")
                return {"status": "ignored", "message": "Không tìm thấy phiếu tương ứng để cập nhật lưu kho"}

        # =========================================================
        # 4B. QUY TRÌNH GIAO NHẬN (WORKFLOW_ID = 20037)
        # =========================================================
        elif workflow_id == WORKFLOW_DELIVERY:
            stage_lower = stage_name.lower().strip()

            # 1. Cột "Thất bại"
            if any(kw in stage_lower for kw in ["thất bại", "hủy giao", "không thành công", "fail", "cancel"]):
                delivery_status = "failed"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "failed",
                    "status": "cancelled",
                    "failure_reason": "Giao nhận thất bại / Khách hủy giao",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }
            # 2. Cột "Hoàn thành"
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
            # 3. Cột "Xử lý đơn hàng" / "Đang giao"
            elif any(kw in stage_lower for kw in ["xử lý", "đang giao", "đi giao", "vận chuyển", "delivering", "shipping", "processing"]):
                delivery_status = "delivering"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "delivering",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }
            # 4. Cột "Thông Tin Đơn" (Mặc định)
            else:
                delivery_status = "pending"
                update_payload = {
                    "delivery_job_id": job_id,
                    "delivery_status": "pending",
                    "current_process": "delivery",
                    "updated_at": now_iso,
                }

            # ✅ Chỉ cập nhật KHI CÓ ẢNH thuộc nhóm giao nhận — KHÔNG GHI ĐÈ dữ liệu cũ
            if delivery_imgs:
                update_payload["delivery_images"] = delivery_imgs

            if existing_ticket:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                logger.info(f"🚚 [GIAO NHẬN] Cập nhật phiếu {ticket_code} -> delivery_status: {delivery_status}")
            else:
                logger.warning(f"⚠️ [GIAO NHẬN] Không tìm thấy phiếu gốc tương ứng cho Job {job_id} ('{task_name}')")
                return {"status": "ignored", "message": "Không tìm thấy phiếu tương ứng để cập nhật giao nhận"}

        # =========================================================
        # 4C. QUY TRÌNH SỬA CHỮA MẶC ĐỊNH (WORKFLOW_ID = 20025)
        # =========================================================
        else:
            job_data = payload.get("job") if isinstance(payload.get("job"), dict) else {}
            base_failed_reason = (
                payload.get("failed_reason")
                or payload.get("fail_reason")
                or payload.get("cancel_reason")
                or job_data.get("failed_reason")
                or job_data.get("fail_reason")
                or ""
            )
            if base_failed_reason and str(base_failed_reason).strip().lower() != "no value":
                base_failed_reason = str(base_failed_reason).strip()
            else:
                base_failed_reason = ""

            base_failed_note = (
                payload.get("failed_note")
                or payload.get("fail_note")
                or payload.get("cancel_note")
                or job_data.get("failed_note")
                or job_data.get("fail_note")
                or ""
            )
            if base_failed_note and str(base_failed_note).strip().lower() != "no value":
                base_failed_note = str(base_failed_note).strip()
            else:
                base_failed_note = ""

            # Trích xuất ghi chú chung
            raw_custom_note = (
                payload.get("custom_dia_chi")
                or payload.get("custom_ghi_chu")
                or payload.get("description")
            )
            if raw_custom_note and str(raw_custom_note).strip().lower() != "no value":
                base_note = str(raw_custom_note).strip()
            else:
                base_note = clean_base_content(payload.get("content_short") or payload.get("note") or "")

            # Trích xuất tình trạng thiết bị do KTV cập nhật
            custom_tinh_trang = payload.get("custom_tinh_trang_thiet_bi") or ""
            if str(custom_tinh_trang).strip().lower() == "no value":
                custom_tinh_trang = ""
            else:
                custom_tinh_trang = str(custom_tinh_trang).strip()

            # Ánh xạ trạng thái sửa chữa
            status_map = await get_dynamic_status_map()
            mapped_status = validate_status(stage_name, status_map, metatype=metatype, state=state)
            if base_failed_reason or base_failed_note:
                mapped_status = "cancelled"

            parsed_info = parse_task_name(task_name, base_note)
            customer_name = parsed_info.get("customer_name", "Khách lẻ").strip() or "Khách lẻ"

            # 🔥 SỬA LỖI SĐT RỖNG: Chuyển chuỗi rỗng "" thành None (NULL trong DB)
            raw_phone = parsed_info.get("phone", "").strip() or (extracted_phone or "")
            clean_phone = normalize_phone(raw_phone) if raw_phone else None
            phone = clean_phone if clean_phone else None

            address = parsed_info.get("address", "").strip()
            note = parsed_info.get("note", "Chưa có yêu cầu").strip() or "Chưa có yêu cầu"
            received_note = parsed_info.get("received_note", "").strip()

            update_payload = {
                "status": mapped_status,
                "current_process": "repair",
                "updated_at": now_iso,
            }

            # ✅ LOGIC MỚI: Mỗi nhóm ảnh vào đúng cột — KHÔNG GỘP CHÉO, KHÔNG GHI ĐÈ
            # Có ảnh tiếp nhận → cập nhật; không có → giữ nguyên cũ
            if received_imgs:
                update_payload["received_images"] = received_imgs
            # Có ảnh sửa chữa → cập nhật; không có → giữ nguyên cũ
            if repair_imgs:
                update_payload["repair_images"] = repair_imgs

            if mapped_status == "returned":
                update_payload["returned_date"] = now_iso

            # Xử lý dữ liệu thất bại / Hủy sửa
            if mapped_status == "cancelled" or base_failed_reason or base_failed_note:
                update_payload["is_fixed"] = False
                update_payload["repair_result"] = "failed"
                cancel_parts = []
                if base_failed_reason:
                    cancel_parts.append(f"Lý do: {base_failed_reason}")
                if base_failed_note:
                    cancel_parts.append(f"Ghi chú: {base_failed_note}")
                cancel_str = " - ".join(cancel_parts) if cancel_parts else "KTV báo sửa không thành công / Hủy sửa"
                update_payload["failure_reason"] = cancel_str
                curr_received = (existing_ticket.get("received_note") or "").strip() if existing_ticket else received_note
                if cancel_str not in curr_received:
                    update_payload["received_note"] = f"{curr_received} [{cancel_str}]" if curr_received else f"[{cancel_str}]"

            if existing_ticket:
                # --- CẬP NHẬT PHIẾU ĐÃ TỒN TẠI ---
                if mapped_status in {"fixed", "returned"}:
                    ktv_info_parts = []
                    if custom_tinh_trang:
                        ktv_info_parts.append(f"Tình trạng KTV báo: {custom_tinh_trang}")
                    if ktv_info_parts:
                        ktv_text = " | ".join(ktv_info_parts)
                        curr_note = (existing_ticket.get("note") or "").strip()
                        if ktv_text not in curr_note:
                            update_payload["note"] = f"{curr_note} | {ktv_text}" if curr_note else ktv_text
                        curr_received = (existing_ticket.get("received_note") or "").strip()
                        if ktv_text not in curr_received:
                            update_payload["received_note"] = f"{curr_received} | {ktv_text}" if curr_received else ktv_text

                await asyncio.to_thread(
                    lambda: supabase.table("repairs").update(update_payload).eq("id", existing_ticket["id"]).execute()
                )
                ticket_code = existing_ticket["code"]
                logger.info(f"🛠️ [SỬA CHỮA] Cập nhật phiếu {ticket_code} -> status: {mapped_status}")
            else:
                # --- TẠO PHIẾU MỚI ---
                if mapped_status in {"fixed", "returned"}:
                    ktv_info_parts = []
                    if custom_tinh_trang:
                        ktv_info_parts.append(f"Tình trạng KTV báo: {custom_tinh_trang}")
                    if ktv_info_parts:
                        ktv_text = " | ".join(ktv_info_parts)
                        note = f"{note} | {ktv_text}" if note else ktv_text
                        received_note = f"{received_note} | {ktv_text}" if received_note else ktv_text

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
                    # ✅ Tạo mới: gán đúng từng nhóm ảnh
                    "received_images": received_imgs,
                    "repair_images": repair_imgs,
                    "delivery_images": delivery_imgs,
                    "storage_images": storage_imgs,
                    "updated_at": now_iso,
                }
                try:
                    await asyncio.to_thread(
                        lambda: supabase.table("repairs").insert(new_record).execute()
                    )
                    logger.info(f"🛠️ [SỬA CHỮA] Tạo phiếu mới {ticket_code} — job_id: {job_id}")
                except Exception as insert_err:
                    if "duplicate" in str(insert_err).lower():
                        logger.info(f"Webhook: Trùng job_id {job_id} -> Tiến hành cập nhật")
                        existing_retry = await asyncio.to_thread(
                            lambda: supabase.table("repairs")
                                .select("id, code, status, note, received_note")
                                .eq("base_job_id", job_id)
                                .limit(1)
                                .execute()
                        )
                        if existing_retry.data:
                            ex = existing_retry.data[0]
                            await asyncio.to_thread(
                                lambda: supabase.table("repairs")
                                    .update(update_payload)
                                    .eq("id", ex["id"])
                                    .execute()
                            )
                            ticket_code = ex["code"]
                        else:
                            raise
                    else:
                        raise

        # =========================================================
        # 5. PHÁT THÔNG BÁO REALTIME QUA WEBSOCKET
        # =========================================================
        try:
            await manager.broadcast({
                "type": "base_sync",
                "workflow_id": workflow_id,
                "job_id": job_id,
                "code": ticket_code,
                "stage_name": stage_name,
            })
        except Exception as ws_err:
            logger.warning(f"Webhook: Broadcast tin nhắn thất bại: {ws_err}")

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