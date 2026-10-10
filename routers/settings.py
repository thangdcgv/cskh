import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import supabase
from routers.auth import get_current_user

router = APIRouter(prefix="/api/settings", tags=["Settings"])


# === Schemas ===
class SystemSettingUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    key: str = Field(..., min_length=1, description="Khóa cấu hình")
    value: str = Field(..., description="Giá trị cấu hình")
    description: Optional[str] = Field(None, description="Mô tả cấu hình")


class StatusMappingCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    base_stage: str = Field(..., min_length=1, description="Giai đoạn gốc")
    system_status: str = Field(..., description="Trạng thái hệ thống tương ứng")
    description: Optional[str] = Field(None, description="Mô tả ghi chú")

    @field_validator("system_status")
    @classmethod
    def validate_system_status(cls, v: str) -> str:
        allowed = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}
        v_clean = v.lower()
        if v_clean not in allowed:
            raise ValueError(
                f"Trạng thái không hợp lệ. Các trạng thái cho phép: {', '.join(sorted(allowed))}"
            )
        return v_clean


# === Phân Quyền Quản Trị ===
async def require_admin(current_user: dict = Depends(get_current_user)):
    """
    Yêu cầu quyền Admin / Super Admin / System Admin đối với các thao tác thay đổi cấu hình.
    """
    role = (current_user.get("role") or "").strip().lower().replace("_", " ")
    allowed_roles = {"admin", "super admin", "system admin", "systemadmin"}

    if role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền thực hiện thao tác quản trị này."
        )
    return current_user


# === Helper ===
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# === 1. Cấu hình Hệ thống (System Configs) ===
@router.get("/config")
async def get_system_configs(_: dict = Depends(get_current_user)):
    """
    Lấy toàn bộ cấu hình hệ thống (Mọi người dùng đã đăng nhập như Lễ tân/User đều đọc được
    để lấy các thông số như `sms_hotline`).
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("*").execute()
        )
        configs = {item["key"]: item["value"] for item in (res.data or [])}
        return configs
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải cấu hình hệ thống: {str(e)}"
        )


@router.post("/config")
async def update_system_config(
    setting: SystemSettingUpdate,
    current_user: dict = Depends(require_admin)
):
    """Tạo mới hoặc cập nhật khóa cấu hình hệ thống (Upsert)"""
    try:
        data = {
            "key": setting.key,
            "value": setting.value,
            "updated_at": utc_now_iso(),
            "updated_by": current_user.get("id") or current_user.get("auth_id")
        }
        if setting.description:
            data["description"] = setting.description

        # Bổ sung on_conflict="key" để ghi đè chính xác
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


# === 2. Ánh Xạ Trạng Thái (Status Mappings) ===
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
    """Thêm mới hoặc cập nhật ánh xạ trạng thái theo base_stage (Chỉ Admin)"""
    try:
        data = {
            "base_stage": mapping.base_stage,
            "system_status": mapping.system_status,
            "description": mapping.description,
            "updated_at": utc_now_iso(),
            "updated_by": current_user.get("id") or current_user.get("auth_id")
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


@router.delete("/status-mappings/{id}")
async def delete_status_mapping(
    id: int,
    current_user: dict = Depends(require_admin)
):
    """Xóa 1 ánh xạ trạng thái theo ID (Chỉ Admin)"""
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