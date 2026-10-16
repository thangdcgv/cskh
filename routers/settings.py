import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import supabase
from routers.auth import get_current_user

router = APIRouter(prefix="/api/settings", tags=["Settings"])

ALLOWED_STATUSES = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}

# Danh sách các Key chuẩn dùng cho Base Workflow & System Sync
DEFAULT_SYSTEM_KEYS = [
    {"key": "base_repair_token", "value": "", "description": "API Key / Token cho ứng dụng Sửa chữa (Repair)"},
    {"key": "base_warranty_token", "value": "", "description": "API Key / Token cho ứng dụng Bảo hành (Warranty)"},
    {"key": "base_recept_token", "value": "", "description": "API Key / Token cho ứng dụng Nhận - Trả (Recept&Return)"},
    {"key": "base_table_token", "value": "", "description": "API Key / Token cho Base Table"},
    {"key": "workflow_repair_id", "value": "20025", "description": "Workflow ID quy trình Sửa chữa trên Base"},
    {"key": "workflow_storage_id", "value": "20036", "description": "Workflow ID quy trình Lưu kho trên Base"},
    {"key": "workflow_delivery_id", "value": "20037", "description": "Workflow ID quy trình Giao nhận trên Base"},
    {"key": "base_shared_secret", "value": "", "description": "Secret Token xác thực Webhook từ Base gửi về"},
    {"key": "cache_ttl_seconds", "value": "60", "description": "Thời gian lưu Cache RAM cấu hình (giây)"},
]


# =========================================================
# SCHEMAS
# =========================================================
class SystemSettingItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(..., min_length=1, max_length=100, description="Khóa cấu hình")
    value: str = Field(..., description="Giá trị cấu hình")
    description: Optional[str] = Field(None, max_length=255, description="Mô tả cấu hình")


class SystemSettingBulkUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    settings: List[SystemSettingItem] = Field(
        ..., min_length=1, description="Danh sách các cấu hình cần cập nhật"
    )


class StatusMappingCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    base_stage: str = Field(..., min_length=1, description="Giai đoạn gốc trên Base")
    system_status: str = Field(..., description="Trạng thái hệ thống tương ứng")
    description: Optional[str] = Field(None, description="Mô tả ghi chú")

    @field_validator("system_status")
    @classmethod
    def validate_system_status(cls, v: str) -> str:
        v_clean = v.lower().strip()
        if v_clean not in ALLOWED_STATUSES:
            raise ValueError(
                f"Trạng thái không hợp lệ. Các trạng thái cho phép: {', '.join(sorted(ALLOWED_STATUSES))}"
            )
        return v_clean


class StatusMappingBulkUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    mappings: List[StatusMappingCreate] = Field(
        ..., min_length=1, description="Danh sách ánh xạ trạng thái cần lưu"
    )


# =========================================================
# PHÂN QUYỀN & HELPER
# =========================================================
async def require_admin(current_user: dict = Depends(get_current_user)):
    """Yêu cầu quyền Admin / Super Admin / System Admin đối với các thao tác quản trị."""
    role = (current_user.get("role") or "").strip().lower().replace("_", " ")
    allowed_roles = {"admin", "super admin", "system admin", "systemadmin"}

    if role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền thực hiện thao tác quản trị này."
        )
    return current_user


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(val: str) -> str:
    """Che giấu chuỗi Token/Secret nếu không phải Admin xem trực tiếp"""
    if not val or len(val) < 8:
        return "********"
    return f"{val[:3]}***{val[-4:]}"


# =========================================================
# 1. CẤU HÌNH HỆ THỐNG (SYSTEM SETTINGS)
# =========================================================
@router.get("/config")
async def get_system_configs(_: dict = Depends(get_current_user)):
    """
    Lấy toàn bộ cấu hình dưới dạng Key-Value { "key": "value" }.
    Tự động che giấu các chuỗi Token/Secret đối với End-User.
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key, value").execute()
        )
        configs = {}
        for item in (res.data or []):
            k = item["key"]
            v = item["value"]
            # Che giấu Token nhạy cảm
            if any(secret_kw in k.lower() for secret_kw in ["token", "secret", "password"]):
                configs[k] = mask_secret(v)
            else:
                configs[k] = v
        return configs
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải cấu hình hệ thống: {str(e)}"
        )


@router.get("/config/detail")
async def get_system_configs_detail(current_user: dict = Depends(require_admin)):
    """
    Lấy danh sách chi tiết các cấu hình (Bao gồm giá trị gốc, description, updated_at).
    Dùng riêng cho Admin UI để hiển thị và chỉnh sửa Form.
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("*").order("key").execute()
        )
        return res.data or []
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải danh sách chi tiết cấu hình: {str(e)}"
        )


@router.post("/config/init-defaults")
async def initialize_default_settings(current_user: dict = Depends(require_admin)):
    """
    Khởi tạo mẫu các Key cấu hình mặc định (Repair Token, Warranty Token, Workflow IDs...)
    Nếu Key đã tồn tại thì giữ nguyên giá trị cũ.
    """
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        now = utc_now_iso()

        # 1. Kiểm tra các key đã có trong DB
        existing_res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key").execute()
        )
        existing_keys = {item["key"] for item in (existing_res.data or [])}

        # 2. Lọc danh sách key chưa tạo
        records_to_insert = []
        for default_item in DEFAULT_SYSTEM_KEYS:
            if default_item["key"] not in existing_keys:
                records_to_insert.append({
                    "key": default_item["key"],
                    "value": default_item["value"],
                    "description": default_item["description"],
                    "updated_at": now,
                    "updated_by": user_id,
                })

        if records_to_insert:
            await asyncio.to_thread(
                lambda: supabase.table("system_settings").insert(records_to_insert).execute()
            )

        return {
            "status": "success",
            "message": f"Đã khởi tạo thành công {len(records_to_insert)} khóa cấu hình mặc định",
            "inserted_keys": [item["key"] for item in records_to_insert]
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi khởi tạo cấu hình mặc định: {str(e)}"
        )


@router.post("/config")
async def update_single_system_config(
    setting: SystemSettingItem,
    current_user: dict = Depends(require_admin)
):
    """Thêm mới hoặc cập nhật 1 khóa cấu hình đơn lẻ (Upsert)"""
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        data = {
            "key": setting.key,
            "value": setting.value,
            "updated_at": utc_now_iso(),
            "updated_by": user_id,
        }
        if setting.description is not None:
            data["description"] = setting.description

        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").upsert(data, on_conflict="key").execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Không thể lưu cấu hình"
            )

        return {
            "status": "success",
            "message": f"Cập nhật cấu hình `{setting.key}` thành công",
            "data": res.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi lưu cấu hình: {str(e)}"
        )


@router.put("/config/bulk")
async def update_bulk_system_configs(
    payload: SystemSettingBulkUpdate,
    current_user: dict = Depends(require_admin)
):
    """Cập nhật hàng loạt nhiều khóa cấu hình cùng lúc (Dùng khi submit Admin Settings Form)"""
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        now = utc_now_iso()

        records = []
        for item in payload.settings:
            record = {
                "key": item.key,
                "value": item.value,
                "updated_at": now,
                "updated_by": user_id,
            }
            if item.description is not None:
                record["description"] = item.description
            records.append(record)

        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").upsert(records, on_conflict="key").execute()
        )

        return {
            "status": "success",
            "message": f"Đã cập nhật {len(records)} cấu hình thành công",
            "data": res.data or []
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi cập nhật cấu hình hàng loạt: {str(e)}"
        )


@router.delete("/config/{key}")
async def delete_system_config(
    key: str,
    current_user: dict = Depends(require_admin)
):
    """Xóa 1 khóa cấu hình khỏi hệ thống"""
    try:
        target = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key").eq("key", key).execute()
        )
        if not target.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy khóa cấu hình `{key}`"
            )

        await asyncio.to_thread(
            lambda: supabase.table("system_settings").delete().eq("key", key).execute()
        )
        return {
            "status": "success",
            "message": f"Đã xóa cấu hình `{key}` thành công"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi xóa cấu hình: {str(e)}"
        )


# =========================================================
# 2. ÁNH XẠ TRẠNG THÁI (STATUS MAPPINGS)
# =========================================================
@router.get("/status-mappings")
async def get_status_mappings(_: dict = Depends(get_current_user)):
    """Lấy danh sách ánh xạ trạng thái hệ thống"""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("status_mappings").select("*").order("id").execute()
        )
        return res.data or []
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải danh sách ánh xạ: {str(e)}"
        )


@router.post("/status-mappings")
async def save_status_mapping(
    mapping: StatusMappingCreate,
    current_user: dict = Depends(require_admin)
):
    """Thêm mới hoặc cập nhật 1 ánh xạ trạng thái theo base_stage (Upsert)"""
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        data = {
            "base_stage": mapping.base_stage,
            "system_status": mapping.system_status,
            "description": mapping.description,
            "updated_at": utc_now_iso(),
            "updated_by": user_id,
        }

        res = await asyncio.to_thread(
            lambda: supabase.table("status_mappings").upsert(data, on_conflict="base_stage").execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Không thể lưu ánh xạ trạng thái"
            )

        return {
            "status": "success",
            "message": f"Đã lưu ánh xạ cho giai đoạn `{mapping.base_stage}` thành công",
            "data": res.data[0]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi lưu ánh xạ: {str(e)}"
        )


@router.put("/status-mappings/bulk")
async def save_bulk_status_mappings(
    payload: StatusMappingBulkUpdate,
    current_user: dict = Depends(require_admin)
):
    """Cập nhật hàng loạt nhiều ánh xạ trạng thái cùng lúc cho Admin UI"""
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        now = utc_now_iso()

        records = []
        for item in payload.mappings:
            records.append({
                "base_stage": item.base_stage,
                "system_status": item.system_status,
                "description": item.description,
                "updated_at": now,
                "updated_by": user_id,
            })

        res = await asyncio.to_thread(
            lambda: supabase.table("status_mappings").upsert(records, on_conflict="base_stage").execute()
        )

        return {
            "status": "success",
            "message": f"Đã cập nhật {len(records)} ánh xạ trạng thái thành công",
            "data": res.data or []
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi lưu ánh xạ hàng loạt: {str(e)}"
        )


@router.delete("/status-mappings/{id}")
async def delete_status_mapping(
    id: int,
    current_user: dict = Depends(require_admin)
):
    """Xóa 1 ánh xạ trạng thái theo ID"""
    try:
        target = await asyncio.to_thread(
            lambda: supabase.table("status_mappings").select("id, base_stage").eq("id", id).execute()
        )
        if not target.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Không tìm thấy ánh xạ cần xóa"
            )

        await asyncio.to_thread(
            lambda: supabase.table("status_mappings").delete().eq("id", id).execute()
        )
        return {
            "status": "success",
            "message": f"Đã xóa ánh xạ `{target.data[0]['base_stage']}` thành công"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi xóa ánh xạ: {str(e)}"
        )