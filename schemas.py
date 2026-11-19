import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Regex SĐT di động Việt Nam chuẩn (10 chữ số, đầu 03, 05, 07, 08, 09)
PHONE_REGEX = re.compile(r'^0[35789]\d{8}$')


def normalize_phone(phone: Optional[str]) -> str:
    """Chuẩn hóa số điện thoại về dạng 0xxxxxxxxx"""
    if not phone:
        return ""
    # Chỉ giữ lại chữ số
    digits = re.sub(r'\D', '', phone)

    # Chuyển đầu 84 (11 chữ số) thành đầu 0 (10 chữ số)
    if digits.startswith('84') and len(digits) == 11:
        digits = '0' + digits[2:]

    return digits


class ReceiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    customer_name: str = Field(..., min_length=2, max_length=100, description="Họ và tên khách hàng")
    phone: str = Field(..., description="Số điện thoại (hỗ trợ 0/84/+84)")
    address: Optional[str] = Field(None, max_length=255, description="Địa chỉ khách hàng")
    note: Optional[str] = Field(None, description="Yêu cầu / ghi chú chung")
    received_note: Optional[str] = Field(None, description="Tình trạng máy & phụ kiện khi nhận")
    base_job_id: Optional[str] = Field(None, max_length=50, description="Mã công việc hệ thống ngoài")

    @field_validator("customer_name", mode="after")
    def validate_customer_name(cls, v: str) -> str:
        if len(v) < 2:
            raise ValueError("Tên khách hàng phải có ít nhất 2 ký tự")
        return v

    @field_validator("phone", mode="before")
    def validate_and_normalize_phone(cls, v: str) -> str:
        cleaned = normalize_phone(v)
        if not PHONE_REGEX.match(cleaned):
            raise ValueError("Số điện thoại không hợp lệ (VD: 0912345678, 84912345678 hoặc +84912345678)")
        return cleaned


class ReturnRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    returned_note: Optional[str] = Field(None, description="Ghi chú khi trả máy")
    returned_method: Optional[str] = Field("Tại cửa hàng", max_length=50, description="Hình thức hoàn trả")

    @field_validator("returned_method", mode="after")
    def clean_returned_method(cls, v: Optional[str]) -> str:
        if not v:
            return "Tại cửa hàng"
        return v


#PHẦN CỦA DỌN MÁY
class CleanedPrinterCreate(BaseModel):
    model_name: str
    classification: str
    quantity: int = 1
    status: str = "Sẵn có"
    ghi_chu: Optional[str] = None

class CleanedPrinterUpdate(BaseModel):
    model_name: Optional[str] = None
    classification: Optional[str] = None
    quantity: Optional[int] = None
    status: Optional[str] = None
    ghi_chu: Optional[str] = None

class CleanedPrinterStatusUpdate(BaseModel):
    status: str
    ghi_chu: Optional[str] = None

class CleanedPrinterResponse(BaseModel):
    id: int
    model_name: str
    classification: str
    quantity: int
    status: str
    department: Optional[str] = None
    ghi_chu: Optional[str] = None
    created_by: str
    updated_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class CleanedPrinterMoveRequest(BaseModel):
    quantity: int = 1
    target_status: str
    ghi_chu: Optional[str] = None
    clear_note: bool = False