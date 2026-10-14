import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import supabase
from routers.auth import get_current_user

router = APIRouter(prefix="/api/settings", tags=["Settings"])


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
        allowed = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}
        v_clean = v.lower().strip()
        if v_clean not in allowed:
            raise ValueError(
                f"Trạng thái không hợp lệ. Các trạng thái cho phép: {', '.join(sorted(allowed))}"
            )
        return v_clean


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


# =========================================================
# 1. CẤU HÌNH HỆ THỐNG (SYSTEM SETTINGS)
# =========================================================
@router.get("/config")
async def get_system_configs(_: dict = Depends(get_current_user)):
    """
    Lấy toàn bộ cấu hình dưới dạng Key-Value { "key": "value" }.
    Dùng cho các Client/User cần đọc nhanh cấu hình hệ thống.
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key, value").execute()
        )
        configs = {item["key"]: item["value"] for item in (res.data or [])}
        return configs
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải cấu hình hệ thống: {str(e)}"
        )


@router.get("/config/detail")
async def get_system_configs_detail(current_user: dict = Depends(require_admin)):
    """
    Lấy danh sách chi tiết các cấu hình (Gồm description, updated_at, updated_by).
    Dùng riêng cho trang Quản trị Admin UI.
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
    """
    Cập nhật hàng loạt nhiều khóa cấu hình cùng lúc (Dùng khi submit Admin Settings Form).
    """
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
    """Thêm mới hoặc cập nhật ánh xạ trạng thái theo base_stage (Upsert)"""
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