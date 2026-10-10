import asyncio
import html
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Request

from database import supabase
from utils import parse_task_name, generate_ticket_code
from websocket_manager import manager

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/webhook", tags=["Webhook"])

# Cho phép các trạng thái hợp lệ trong hệ thống
ALLOWED_STATUSES = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}

# Cache toàn cục cho Status Mappings & Secret Key
_status_map_cache: Optional[dict] = None
_status_map_updated: float = 0

_secret_cache: Optional[str] = None
_secret_updated: float = 0

_CACHE_TTL = 60  # Giây


def clean_base_content(raw_text: str) -> str:
    """Làm sạch chuỗi gộp content_short của Base nếu không có custom field"""
    if not raw_text or str(raw_text).strip().lower() == "no value":
        return ""
    
    text = html.unescape(str(raw_text))
    text = re.sub(r'Hình ảnh[^\n·•]*:?\s*\[\d+\s*files?\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\s*Ghi chú:\s*', '', text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r'[·•|]+', text) if p.strip()]
    return " - ".join(parts)


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

    # Danh sách ánh xạ mặc định chuẩn xác theo quy trình Base thực tế
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


def _validate_status(raw_status: str, status_map: dict, metatype: str = "", state: str = "") -> str:
    """Chuẩn hóa & ánh xạ trạng thái chính xác theo thứ tự ưu tiên"""
    normalized = raw_status.strip().lower()
    
    # 1. Ưu tiên 1: Khớp chính xác theo Bảng ánh xạ trong DB / Dictionary
    if normalized in status_map:
        mapped = status_map[normalized]
        return "processing" if mapped == "repairing" else mapped

    # 2. Ưu tiên 2: Khớp cờ hoàn thành / hủy từ Base
    if metatype == "done" or state == "done":
        return "fixed"
    if metatype in {"failed", "cancelled"} or state in {"failed", "cancelled"}:
        return "cancelled"

    # 3. Ưu tiên 3: Bắt từ khóa TIẾP NHẬN / TẠO ĐƠN BAN ĐẦU
    if any(kw in normalized for kw in ["thông tin đơn", "tiếp nhận", "nhận máy", "tạo mới", "mới", "reception", "received"]):
        return "received"

    # 4. Ưu tiên 4: Bắt từ khóa HOÀN THÀNH
    if any(kw in normalized for kw in ["hoàn thành", "sửa xong", "xong", "đã sửa", "done", "finished", "hoàn tất"]):
        return "fixed"

    # 5. Ưu tiên 5: Bắt từ khóa ĐANG SỬA CHỮA (Dùng cụm rõ ràng, tránh từ "sửa" đứng đơn)
    if any(kw in normalized for kw in ["tiến hành", "sửa chữa", "đang sửa", "xử lý", "processing"]):
        return "processing"

    # 6. Ưu tiên 6: Bắt từ khóa TRẢ MÁY
    if any(kw in normalized for kw in ["trả máy", "trả", "bàn giao", "returned"]):
        return "returned"

    # 7. Ưu tiên 7: Bắt từ khóa HỦY
    if any(kw in normalized for kw in ["hủy", "cancel", "failed"]):
        return "cancelled"

    return "received"


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
        else:
            logger.warning("Webhook: Chưa cấu hình base_shared_secret — Bỏ qua xác thực!")

        # 3. Trích xuất thông tin Job & Stage
        job_id = str(payload.get("job_id") or payload.get("id", "")).strip()
        if not job_id or job_id.lower() == "none":
            return {"status": "ignored", "message": "Thiếu job_id hợp lệ", "code": None}

        task_name = payload.get("task_name") or payload.get("name") or payload.get("job_name", "")

        state = str(payload.get("state") or "").strip().lower()
        metatype = ""

        stage_obj = payload.get("stage")
        if isinstance(stage_obj, str):
            try:
                stage_obj = json.loads(stage_obj)
            except Exception:
                pass

        if isinstance(stage_obj, dict):
            stage_name = stage_obj.get("name", "Tiếp nhận")
            metatype = str(stage_obj.get("metatype") or "").strip().lower()
        else:
            stage_name = payload.get("stage_name") or str(stage_obj or "Tiếp nhận")

        # Trích xuất ghi chú từ custom_dia_chi hoặc content_short
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


        # 4. Ánh xạ trạng thái chính xác
        status_map = await get_dynamic_status_map()
        mapped_status = _validate_status(stage_name, status_map, metatype=metatype, state=state)

        # 5. Phân tích thông tin khách hàng từ Task Name & Base Note
        parsed_info = parse_task_name(task_name, base_note)
        customer_name = parsed_info.get("customer_name", "Khách lẻ").strip() or "Khách lẻ"
        phone = parsed_info.get("phone", "").strip()
        address = parsed_info.get("address", "").strip()
        note = parsed_info.get("note", "Chưa có yêu cầu").strip() or "Chưa có yêu cầu"
        received_note = parsed_info.get("received_note", "").strip()

        # 6. Kiểm tra sự tồn tại của phiếu trong DB
        existing_res = await asyncio.to_thread(
            lambda: supabase.table("repairs")
            .select("id, code, status, note, received_note")
            .eq("base_job_id", job_id)
            .limit(1)
            .execute()
        )
        existing = existing_res.data[0] if existing_res.data else None

        now_iso = datetime.now(timezone.utc).isoformat()
        
        update_payload = {
            "status": mapped_status,
            "updated_at": now_iso,
        }
        if mapped_status == "returned":
            update_payload["returned_date"] = now_iso

        ticket_code = None

        if existing:
            # --- CẬP NHẬT PHIẾU ĐÃ TỒN TẠI ---
            if mapped_status == "processing":
                pass
            elif mapped_status in {"fixed", "returned"}:
                ktv_info_parts = []
                if custom_tinh_trang:
                    ktv_info_parts.append(f"Tình trạng KTV báo: {custom_tinh_trang}")
                
                if ktv_info_parts:
                    ktv_text = " | ".join(ktv_info_parts)
                    
                    curr_note = (existing.get("note") or "").strip()
                    if ktv_text not in curr_note:
                        update_payload["note"] = f"{curr_note} | {ktv_text}" if curr_note else ktv_text

                    curr_received = (existing.get("received_note") or "").strip()
                    if ktv_text not in curr_received:
                        update_payload["received_note"] = f"{curr_received} | {ktv_text}" if curr_received else ktv_text

            await asyncio.to_thread(
                lambda: supabase.table("repairs")
                .update(update_payload)
                .eq("id", existing["id"])
                .execute()
            )
            ticket_code = existing["code"]
            logger.info(f"Webhook: Cập nhật phiếu {ticket_code} — Trạng thái mới: {mapped_status} (Giai đoạn Base: '{stage_name}')")

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

            ticket_code = generate_ticket_code()
            new_record = {
                "base_job_id": job_id,
                "code": ticket_code,
                "customer_name": customer_name,
                "phone": phone,
                "address": address,
                "note": note,
                "received_note": received_note,
                "status": mapped_status,
                "returned_date": update_payload.get("returned_date"),
                "updated_at": now_iso,
            }
            try:
                await asyncio.to_thread(
                    lambda: supabase.table("repairs").insert(new_record).execute()
                )
                logger.info(f"Webhook: Tạo phiếu mới {ticket_code} — job_id: {job_id}")
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

        # 7. Phát thông báo Realtime tới Client qua WebSocket
        try:
            await manager.broadcast({
                "type": "base_sync",
                "job_id": job_id,
                "code": ticket_code,
                "status": mapped_status,
                "stage_name": stage_name,
            })
        except Exception as ws_err:
            logger.warning(f"Webhook: Broadcast tin nhắn thất bại: {ws_err}")

        return {
            "status": "success",
            "message": "Đồng bộ webhook thành công",
            "code": ticket_code,
            "status_new": mapped_status,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Webhook Error] {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Lỗi hệ thống khi xử lý webhook")