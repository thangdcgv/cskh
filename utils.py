import re
import random
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

# Múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

# Tiền tố + độ dài cấu hình mã phiếu
TICKET_PREFIX = "DT"
TICKET_SUFFIX_LENGTH = 5

# Regex SĐT Việt Nam chuẩn
PHONE_PATTERN = re.compile(
    r'(?:(?:\+?84)|0)?'          # Tiền tố quốc gia +84 / 84 hoặc 0
    r'([35789]\d{8})'            # Đầu số di động + 8 số đuôi
)


def normalize_phone(phone: Optional[str]) -> str:
    """Chuẩn hóa SĐT về dạng 0xxxxxxxxx (10 chữ số)"""
    if not phone:
        return ""
    digits = re.sub(r'\D', '', phone)
    
    if digits.startswith('84') and len(digits) == 11:
        digits = '0' + digits[2:]
    elif len(digits) == 9 and digits.startswith('9'):
        digits = '0' + digits

    return digits if len(digits) == 10 and digits.startswith('0') else ""


def parse_task_name(task_name: str, base_note: str = "") -> Dict[str, str]:
    """
    Tách thông tin thông minh dựa trên mốc SĐT (Pivot):
    - TRƯỚC SĐT -> Họ tên khách hàng (Bảo toàn ghi chú xe trong ngoặc)
    - SĐT       -> Chuẩn hóa SĐT
    - SAU SĐT   -> Địa chỉ / Chành xe / Tỉnh thành
    - note      -> Mặc định "Chưa có yêu cầu" (để nhân viên tự chỉnh sửa)
    """
    clean_base_note = base_note.strip() if base_note else ""

    if not task_name or not task_name.strip():
        return {
            "customer_name": "Khách lẻ",
            "phone": "",
            "address": "",
            "note": "Chưa có yêu cầu",
            "received_note": clean_base_note
        }

    raw_text = task_name.strip()
    phone = ""
    customer_name = "Khách lẻ"
    address = ""

    # 1. Tìm vị trí SĐT làm mốc phân cách trung tâm
    phone_match = PHONE_PATTERN.search(raw_text)

    if phone_match:
        raw_phone = phone_match.group(0)
        phone = normalize_phone(raw_phone)
        start_idx, end_idx = phone_match.span()

        # Đoạn TRƯỚC SĐT -> Tên khách hàng (+ thông tin trong ngoặc)
        left_part = raw_text[:start_idx].strip()
        # Loại bỏ các dấu phân cách thừa ở cuối đoạn bên trái (+ - | / , ;)
        left_part = re.sub(r'[\+\-\|\/,;\s]+$', '', left_part).strip()
        if left_part:
            customer_name = left_part

        # Đoạn SAU SĐT -> Khu vực / Tỉnh thành / Chành xe
        right_part = raw_text[end_idx:].strip()
        # Loại bỏ các dấu phân cách thừa ở đầu đoạn bên phải (+ - | / , ;)
        right_part = re.sub(r'^[\+\-\|\/,;\s]+', '', right_part).strip()
        if right_part:
            address = right_part

    else:
        # Trường hợp không có SĐT trong Task Name
        separators = r'[\+\-\|\/,;]+'
        segments = [s.strip() for s in re.split(separators, raw_text) if s.strip()]
        if len(segments) >= 2:
            customer_name = segments[0]
            address = " - ".join(segments[1:])
        elif len(segments) == 1:
            customer_name = segments[0]

    if not customer_name or customer_name.isdigit():
        customer_name = "Khách lẻ"

    return {
        "customer_name": customer_name,
        "phone": phone,
        "address": address,
        "note": "Chưa có yêu cầu",
        "received_note": clean_base_note
    }


def generate_ticket_code() -> str:
    """
    Tạo mã phiếu theo múi giờ Việt Nam: DT-MMDD-XXXXX (VD: DT1009-47291)
    """
    date_str = datetime.now(VN_TZ).strftime("%m%d")
    low = 10 ** (TICKET_SUFFIX_LENGTH - 1)
    high = (10 ** TICKET_SUFFIX_LENGTH) - 1
    
    suffix = random.randint(low, high)
    return f"{TICKET_PREFIX}{date_str}-{suffix}"