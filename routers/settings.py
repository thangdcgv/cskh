import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import supabase_admin as supabase
from routers.auth import get_current_user
from routers.permissions import can_access

router = APIRouter(prefix="/api/settings", tags=["Settings"])

ALLOWED_STATUSES = {"received", "processing", "repairing", "fixed", "returned", "cancelled"}
ALLOWED_TERM_TYPES = {"VALID", "INVALID", "NOTE"}

# Danh sách các Key chuẩn dùng cho Base Workflow, System Sync & Trang Bảo hành
DEFAULT_SYSTEM_KEYS = [
    # --- System & Base Sync Tokens ---
    {"key": "base_repair_token", "value": "", "description": "API Key / Token cho ứng dụng Sửa chữa (Repair)"},
    {"key": "base_storage_token", "value": "", "description": "API Key / Token cho ứng dụng Lưu kho (Storage)"},
    {"key": "base_delivery_token", "value": "", "description": "API Key / Token cho ứng dụng Giao nhận (Delivery)"},
    {"key": "base_warranty_token", "value": "", "description": "API Key / Token cho ứng dụng Bảo hành (Warranty)"},
    {"key": "base_recept_token", "value": "", "description": "API Key / Token cho ứng dụng Nhận - Trả (Recept&Return)"},
    {"key": "base_table_token", "value": "", "description": "API Key / Token cho Base Table"},
    {"key": "workflow_repair_id", "value": "18190", "description": "Workflow ID quy trình Sửa chữa trên Base"},
    {"key": "workflow_storage_id", "value": "18720", "description": "Workflow ID quy trình Lưu kho trên Base"},
    {"key": "workflow_delivery_id", "value": "18249", "description": "Workflow ID quy trình Giao nhận trên Base"},
    {"key": "workflow_install_id", "value": "18191", "description": "Workflow ID quy trình Kỹ thuật lắp đặt trên Base"},
    {"key": "base_shared_secret", "value": "", "description": "Secret Token xác thực Webhook từ Base gửi về"},
    {"key": "cache_ttl_seconds", "value": "60", "description": "Thời gian lưu Cache RAM cấu hình (giây)"},

    # --- Thông tin Doanh nghiệp & Trang bảo hành công khai ---
    {"key": "company_name", "value": "CÔNG TY TNHH MÁY IN ĐẠI THÀNH", "description": "Tên công ty hiển thị footer/header"},
    {"key": "company_address", "value": "29/31 Nguyễn Văn Khối, P. Thông Tây Hội, TP.HCM", "description": "Địa chỉ trụ sở công ty"},
    {"key": "hotline_warranty", "value": "0795570381", "description": "Hotline Kỹ thuật bảo hành"},
    {"key": "hotline_repair", "value": "0795570382", "description": "Hotline Kỹ thuật sửa chữa"},
    {"key": "hotline_feedback", "value": "0938580406", "description": "Hotline Phản ánh dịch vụ / Quản lý"},
    {"key": "website_main", "value": "https://www.mayindaithanh.com", "description": "Website chính thức 1"},
    {"key": "website_sub", "value": "https://www.mayinsaigon.com", "description": "Website chính thức 2"},
    {"key": "guide_url", "value": "https://printer-guide.onrender.com", "description": "Đường dẫn Thư viện Hướng dẫn & Xử lý lỗi"},
    {"key": "logo_url", "value": "https://ejbillnpxxiubbiyyaxn.supabase.co/storage/v1/object/public/publicLogo/LOGO.png", "description": "Đường dẫn Logo công ty hiển thị trên giao diện"},
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


class WarrantyTermItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: Optional[str] = Field(None, description="UUID điều khoản (nếu cập nhật)")
    term_type: str = Field(..., description="Loại điều khoản: VALID, INVALID, NOTE")
    content: str = Field(..., min_length=1, description="Nội dung điều khoản")
    display_order: int = Field(0, ge=0, description="Thứ tự hiển thị")
    is_active: bool = Field(True, description="Trạng thái kích hoạt")

    @field_validator("term_type")
    @classmethod
    def validate_term_type(cls, v: str) -> str:
        v_clean = v.upper().strip()
        if v_clean not in ALLOWED_TERM_TYPES:
            raise ValueError(
                f"Loại điều khoản không hợp lệ. Các loại hợp lệ: {', '.join(sorted(ALLOWED_TERM_TYPES))}"
            )
        return v_clean


# =========================================================
# PHÂN QUYỀN & HELPER (100% THEO CENTER HUB PERMISSIONS)
# =========================================================
async def require_settings_permission(current_user: dict = Depends(get_current_user)):
    """Kiểm tra quyền truy cập trang & thao tác cấu hình qua ma trận Center Hub."""
    allowed = await asyncio.to_thread(can_access, "settings", current_user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền thực hiện thao tác cấu hình theo phân quyền hệ thống."
        )
    return current_user


# Giữ alias để tương thích nếu có router khác import require_admin
require_admin = require_settings_permission


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask_secret(val: str) -> str:
    """Che giấu chuỗi Token/Secret khi trả qua endpoint chung."""
    if not val:
        return ""
    if len(val) < 8:
        return "********"
    return f"{val[:3]}***{val[-4:]}"


# =========================================================
# 1. CẤU HÌNH HỆ THỐNG (SYSTEM SETTINGS)
# =========================================================
@router.get("/public-config")
async def get_public_system_configs():
    """
    Lấy các thông tin cấu hình công khai (Tên công ty, Hotline, Logo, Địa chỉ, Website).
    API này KHÔNG YÊU CẦU ĐĂNG NHẬP, phục vụ trực tiếp cho trang Tra cứu bảo hành.
    """
    PUBLIC_KEYS = {
        "company_name",
        "company_address",
        "hotline_warranty",
        "hotline_repair",
        "hotline_feedback",
        "website_main",
        "website_sub",
        "guide_url",
        "logo_url",
    }
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key, value").in_("key", list(PUBLIC_KEYS)).execute()
        )
        configs = {item["key"]: item["value"] for item in (res.data or [])}
        return configs
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải cấu hình công khai: {str(e)}"
        )


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
async def get_system_configs_detail(current_user: dict = Depends(require_settings_permission)):
    """
    Lấy danh sách chi tiết các cấu hình (Bao gồm giá trị gốc, description, updated_at).
    Dùng cho Settings UI để hiển thị và chỉnh sửa Form.
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
async def initialize_default_settings(current_user: dict = Depends(require_settings_permission)):
    """
    Khởi tạo mẫu các Key cấu hình mặc định (Tokens, Workflow IDs, Thông tin Công ty...)
    Nếu Key đã tồn tại thì giữ nguyên giá trị cũ.
    """
    try:
        user_id = str(current_user.get("id") or current_user.get("auth_id") or "")
        now = utc_now_iso()

        existing_res = await asyncio.to_thread(
            lambda: supabase.table("system_settings").select("key").execute()
        )
        existing_keys = {item["key"] for item in (existing_res.data or [])}

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
    current_user: dict = Depends(require_settings_permission)
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
    current_user: dict = Depends(require_settings_permission)
):
    """Cập nhật hàng loạt nhiều khóa cấu hình cùng lúc (Dùng khi submit Settings Form)"""
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
    current_user: dict = Depends(require_settings_permission)
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
    current_user: dict = Depends(require_settings_permission)
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
    current_user: dict = Depends(require_settings_permission)
):
    """Cập nhật hàng loạt nhiều ánh xạ trạng thái cùng lúc"""
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
    current_user: dict = Depends(require_settings_permission)
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


# =========================================================
# 3. ĐIỀU KHOẢN BẢO HÀNH (WARRANTY TERMS)
# =========================================================
@router.get("/warranty-terms")
async def get_warranty_terms():
    """
    Lấy danh sách các điều khoản & lưu ý bảo hành đang kích hoạt (is_active = True).
    Public API dùng cho trang tra cứu bảo hành.
    """
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("warranty_terms")
            .select("*")
            .eq("is_active", True)
            .order("display_order", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải danh sách điều khoản bảo hành: {str(e)}"
        )


@router.get("/warranty-terms/admin")
async def get_warranty_terms_admin(current_user: dict = Depends(require_settings_permission)):
    """Lấy toàn bộ danh sách điều khoản bảo hành bao gồm cả bị ẩn (Admin UI)"""
    try:
        res = await asyncio.to_thread(
            lambda: supabase.table("warranty_terms")
            .select("*")
            .order("term_type")
            .order("display_order", desc=False)
            .execute()
        )
        return res.data or []
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi tải danh sách điều khoản bảo hành admin: {str(e)}"
        )


@router.post("/warranty-terms")
async def save_warranty_term(
    term: WarrantyTermItem,
    current_user: dict = Depends(require_settings_permission)
):
    """Thêm mới hoặc cập nhật 1 điều khoản bảo hành (VALID, INVALID, NOTE)"""
    try:
        data = {
            "term_type": term.term_type,
            "content": term.content,
            "display_order": term.display_order,
            "is_active": term.is_active,
        }
        if term.id:
            data["id"] = term.id

        res = await asyncio.to_thread(
            lambda: supabase.table("warranty_terms").upsert(data).execute()
        )
        return {
            "status": "success",
            "message": "Đã lưu điều khoản bảo hành thành công",
            "data": res.data[0] if res.data else None
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi lưu điều khoản bảo hành: {str(e)}"
        )


@router.delete("/warranty-terms/{term_id}")
async def delete_warranty_term(
    term_id: str,
    current_user: dict = Depends(require_settings_permission)
):
    """Xóa 1 điều khoản bảo hành theo UUID"""
    try:
        await asyncio.to_thread(
            lambda: supabase.table("warranty_terms").delete().eq("id", term_id).execute()
        )
        return {
            "status": "success",
            "message": "Đã xóa điều khoản bảo hành thành công"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi xóa điều khoản bảo hành: {str(e)}"
        )
