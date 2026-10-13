import base64
import html
import json
import hashlib
import logging
import random
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("uvicorn.error")

# =========================================================
# BỘ CẤU HÌNH & HẰNG SỐ (CONSTANTS)
# =========================================================
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


# =========================================================
# HÀM XỬ LÝ CHUẨN HÓA & TRÍCH XUẤT
# =========================================================
def normalize_phone(phone: Optional[str]) -> str:
    """Chuẩn hóa SĐT về dạng 0xxxxxxxxx (10 chữ số)"""
    if not phone:
        return ""
    digits = re.sub(r'\D', '', str(phone))
    
    if digits.startswith('84') and len(digits) == 11:
        digits = '0' + digits[2:]
    elif len(digits) == 9 and digits.startswith('9'):
        digits = '0' + digits

    return digits if len(digits) == 10 and digits.startswith('0') else ""


def generate_ticket_code() -> str:
    """
    Tạo mã phiếu ngẫu nhiên theo múi giờ Việt Nam: DT-MMDD-XXXXX (VD: DT1009-47291)
    """
    date_str = datetime.now(VN_TZ).strftime("%m%d")
    low = 10 ** (TICKET_SUFFIX_LENGTH - 1)
    high = (10 ** TICKET_SUFFIX_LENGTH) - 1
    
    suffix = random.randint(low, high)
    return f"{TICKET_PREFIX}{date_str}-{suffix}"


def clean_base_content(raw_text: str) -> str:
    """Làm sạch chuỗi gộp content_short của Base nếu không có custom field"""
    if not raw_text or str(raw_text).strip().lower() == "no value":
        return ""
    
    text = html.unescape(str(raw_text))
    text = re.sub(r'Hình ảnh[^\n·•]*:?\s*\[\d+\s*files?\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^\s*Ghi chú:\s*', '', text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r'[·•|]+', text) if p.strip()]
    return " - ".join(parts)


def extract_identifier_code_or_phone(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Bóc tách Mã phiếu (NT-XXXX-XXX hoặc DT-XXXX-XXXXX) hoặc Số điện thoại từ tiêu đề Job trên Base"""
    if not text:
        return None, None
    
    # Bắt mã phiếu định dạng NT-XXXX-XXX hoặc DT-XXXX-XXXXX
    code_match = re.search(r'(?:NT|DT)-\d{4}-\d{3,5}', text, re.IGNORECASE)
    code = code_match.group(0).upper() if code_match else None

    # Bắt số điện thoại (10-11 chữ số)
    phone_match = re.search(r'0[35789]\d{8,9}', text)
    phone = phone_match.group(0) if phone_match else None

    return code, phone


def validate_status(raw_status: str, status_map: dict, metatype: str = "", state: str = "") -> str:
    """
    Chuẩn hóa & ánh xạ trạng thái chính xác theo thứ tự ưu tiên:
        1. Ánh xạ từ status_map động
        2. Kiểm tra metatype / state trực tiếp
        3. Tìm từ khóa trong tên stage
        4. Mặc định: "received"
    """
    normalized = (raw_status or "").strip().lower()

    # 🔹 Bước 1: Ánh xạ theo bản đồ ưu tiên cao nhất
    if normalized in status_map:
        mapped = status_map[normalized]
        return "processing" if mapped == "repairing" else mapped

    # 🔹 Bước 2: Kiểm tra trực tiếp metatype / state
    if metatype == "done" or state == "done":
        return "fixed"
    if metatype in {"failed", "cancelled", "archived", "deleted"} or state in {"failed", "cancelled", "archived", "deleted"}:
        return "cancelled"

    # 🔹 Bước 3: Dò từ khóa trong tên stage
    if any(kw in normalized for kw in [
        "thông tin đơn", "tiếp nhận", "nhận máy", "tạo mới", "mới",
        "reception", "received"
    ]):
        return "received"
    if any(kw in normalized for kw in [
        "hoàn thành", "sửa xong", "xong", "đã sửa", "done",
        "finished", "hoàn tất"
    ]):
        return "fixed"
    if any(kw in normalized for kw in [
        "tiến hành", "sửa chữa", "đang sửa", "xử lý", "processing"
    ]):
        return "processing"
    if any(kw in normalized for kw in [
        "trả máy", "trả", "bàn giao", "returned"
    ]):
        return "returned"
    if any(kw in normalized for kw in [
        "hủy", "cancel", "failed", "deleted", "archive"
    ]):
        return "cancelled"

    # 🔹 Bước 4: Mặc định khi không khớp gì
    return "received"


def parse_task_name(task_name: str, base_note: str = "") -> Dict[str, str]:
    """
    Tách thông tin thông minh dựa trên mốc SĐT (Pivot):
    - TRƯỚC SĐT -> Họ tên khách hàng (Bảo toàn ghi chú xe trong ngoặc)
    - SĐT       -> Chuẩn hóa SĐT
    - SAU SĐT   -> Địa chỉ / Chành xe / Tỉnh thành
    - note      -> Mặc định "Chưa có yêu cầu"
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

    # Tìm vị trí SĐT làm mốc phân cách trung tâm
    phone_match = PHONE_PATTERN.search(raw_text)

    if phone_match:
        raw_phone = phone_match.group(0)
        phone = normalize_phone(raw_phone)
        start_idx, end_idx = phone_match.span()

        # Đoạn TRƯỚC SĐT -> Tên khách hàng
        left_part = raw_text[:start_idx].strip()
        left_part = re.sub(r'[\+\-\|\/,;\s]+$', '', left_part).strip()
        if left_part:
            customer_name = left_part

        # Đoạn SAU SĐT -> Khu vực / Tỉnh thành / Chành xe / Địa chỉ
        right_part = raw_text[end_idx:].strip()
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

def _build_base_cdn_url(fid: str) -> str:
    """Dựng URL CDN công khai của Base.vn từ fid (đã verify 100%)."""
    parts = fid.split(":")
    if len(parts) < 8:
        raise ValueError(f"FID không đủ phần (cần >=8): {fid}")
    p1, p2, p3, p4, p5, p6, p7 = parts[:7]
    p8 = ":".join(parts[7:])
    if "." not in p8:
        raise ValueError(f"Phần cuối thiếu dấu chấm: {p8}")
    dir_part, rest = p8.split(".", 1)
    if "-" not in rest:
        raise ValueError(f"Thiếu dấu '-' tách token/filename: {rest}")
    token, filename = rest.rsplit("-", 1)
    md5_1 = hashlib.md5(token.encode()).hexdigest()
    hash_dirs = "/".join(md5_1[i:i+2] for i in range(0, 10, 2))
    md5_2 = hashlib.md5(md5_1.encode()).hexdigest()
    return (f"https://data-gcdn.basecdn.net/{p1}/{p2}/{p3}/{p4}/{p5}/{p6}/{p7}"
            f"/{dir_part}/{hash_dirs}/{md5_2}/{filename}")

def classify_image_field(field_name: str) -> str:
    """
    Phân loại trường ảnh thuộc giai đoạn nào dựa trên tên trường.
    Trả về: received | repair | delivery | storage | other
    """
    name = (field_name or "").strip().lower()

    # 🔹 Nhóm Tiếp nhận / Nhận máy
    if any(k in name for k in [
        "tiep_nhan", "nhan_may", "nhan_hang", "tiepnhan", "nhanmay",
        "received", "reception",
        "tiếp_nhận", "nhận_máy", "nhận_hàng"
    ]):
        return "received"

    # 🔹 Nhóm Sửa chữa / KTV / Kiểm tra / Hoàn thành
    if any(k in name for k in [
        "sua_chua", "kiem_tra", "hoan_thanh", "hoan_tat", "ktv",
        "suachua", "kiemtra", "hoanthanh", "hoantat",
        "repair", "fixed", "done", "finished",
        "sửa_chữa", "kiểm_tra", "hoàn_thành", "hoàn_tất"
    ]):
        return "repair"

    # 🔹 Nhóm Giao nhận / Trả khách / Chành xe
    if any(k in name for k in [
        "giao_nhan", "giao_hang", "tra_khach", "chanh_xe",
        "giaonhan", "giaohang", "trachach", "chanhxe",
        "delivery", "delivered", "returned",
        "giao_nhận", "giao_hàng", "trả_khách", "chành_xe"
    ]):
        return "delivery"

    # 🔹 Nhóm Lưu kho / Bảo quản
    if any(k in name for k in [
        "luu_kho", "bao_quan", "kho",
        "luukho", "baoquan",
        "storage",
        "lưu_kho", "bảo_quản"
    ]):
        return "storage"

    # 🔹 Không khớp nhóm nào
    return "other"

def extract_all_base_images(payload: dict) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    Quét ĐỘNG tất cả các trường kết thúc bằng '-fids' trong payload.
    Trả về:
        1. Dict phân loại: {'received': [...], 'repair': [...], 'delivery': [...], 'storage': [...]}
        2. Dict thô: {'tên_trường': [...URLs...], ...}
    """
    sources = []
    if isinstance(payload, dict):
        sources.append(("root", payload))
    if isinstance(payload.get("custom_fields"), dict):
        sources.append(("custom_fields", payload["custom_fields"]))
    if isinstance(payload.get("job"), dict):
        sources.append(("job", payload["job"]))
        if isinstance(payload["job"].get("custom_fields"), dict):
            sources.append(("job.custom_fields", payload["job"]["custom_fields"]))

    by_field: Dict[str, List[str]] = {}
    classified: Dict[str, List[str]] = {
        "received": [],
        "repair": [],
        "delivery": [],
        "storage": []
    }

    for src_name, src in sources:
        if not isinstance(src, dict):
            continue
        for key, value in src.items():
            key_str = str(key)
            if not key_str.endswith("-fids"):
                continue
            if not value or str(value).strip().lower() in ["no value", "none", ""]:
                continue

            field_clean = key_str[:-5]  # Bỏ đuôi "-fids"

            try:
                raw_b64 = str(value).strip()
                if len(raw_b64) % 4:
                    raw_b64 += "=" * (4 - len(raw_b64) % 4)
                file_ids = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
                if not isinstance(file_ids, list):
                    file_ids = [file_ids]

                field_urls = []
                for fid in file_ids:
                    fid_str = str(fid).strip()
                    if not fid_str:
                        continue
                    if fid_str.startswith(("http://", "https://")):
                        url = fid_str
                    else:
                        try:
                            url = _build_base_cdn_url(fid_str)
                        except Exception as e:
                            logger.error(f"❌ Lỗi dựng CDN cho fid={fid_str[:60]}...: {e}")
                            continue
                    field_urls.append(url)

                by_field[field_clean] = field_urls
                cat = classify_image_field(field_clean)

                if cat in classified:
                    classified[cat].extend(field_urls)
                    logger.info(f"📷 [{src_name}] '{field_clean}' → {cat}: {len(field_urls)} ảnh")
                else:
                    logger.warning(f"⚠️ Không phân loại được trường '{field_clean}' ({len(field_urls)} ảnh)")

            except Exception as e:
                logger.error(f"❌ Lỗi giải mã fids tại trường {key_str} ({src_name}): {e}")

    # Loại bỏ ảnh trùng trong từng nhóm
    for cat in classified:
        seen = set()
        unique = []
        for url in classified[cat]:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        classified[cat] = unique

    total = sum(len(v) for v in classified.values())
    if total > 0:
        logger.info(
            f"✅ Tổng ảnh: nhận={len(classified['received'])} | "
            f"sửa={len(classified['repair'])} | giao={len(classified['delivery'])} | kho={len(classified['storage'])}"
        )

    return classified, by_field