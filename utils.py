import base64
import html
import json
import hashlib
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("uvicorn.error")

# =========================================================
# BỘ CẤU HÌNH & HẰNG SỐ (CONSTANTS)
# =========================================================
VN_TZ = timezone(timedelta(hours=7))

TICKET_PREFIX = "DT"
TICKET_SUFFIX_LENGTH = 5

PHONE_PATTERN = re.compile(
    r'(?:(?:\+?84)|0)?'          # Tiền tố quốc gia +84 / 84 hoặc 0
    r'([35789]\d{8})'            # Đầu số di động + 8 số đuôi
)

# Các nhãn hệ thống / trường đã tách cột riêng không được phép lọt vào ghi chú
_BOILERPLATE_FIELD_PATTERNS = [
    r"khách\s*quen\s*:\s*(?:\[external\]|\[internal\]|[^,·•|\n\-]*)",
    r"phân\s*loại\s*khách\s*hàng\s*:\s*[^,·•|\n\-]*",
    r"tên\s*khách\s*hàng\s*:\s*[^,·•|\n\-]*",
    r"số\s*điện\s*thoại\s*:\s*[^,·•|\n\-]*",
    r"điện\s*thoại\s*:\s*[^,·•|\n\-]*",
    r"sđt\s*:\s*[^,·•|\n\-]*",
    r"địa\s*chỉ\s*:\s*[^,·•|\n\-]*",
    r"thông\s*tin\s*chành\s*xe(?:\s*/\s*chuyển\s*phát)?\s*:\s*[^,·•|\n\-]*",
    r"hình\s*ảnh[^:,\n·•|]*:?\s*\[\d+\s*files?\]",
    r"\[external\]",
    r"\[internal\]",
]

_EMPTY_NOTE_TOKENS = {
    "",
    "chưa ghi chú",
    "chưa ghi nhận",
    "no value",
    "none",
    "null",
}


# =========================================================
# HÀM LÀM SẠCH RÁC & TIỀN TỐ FORWARD TỪ BASE.VN
# =========================================================

def strip_forward_tags(text: Optional[str]) -> str:
    """Loại bỏ toàn bộ tag [FWD], [Fwd], FWD: do Base tự gắn khi chuyển tiếp quy trình."""
    if not text:
        return ""
    cleaned = html.unescape(str(text))
    cleaned = re.sub(r"\[fwd(?::[^\]]*)?\]", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:^|(?<=[\s\-|/,;]))fwd\s*:\s*", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s\-|/,;:]+", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def strip_base_boilerplate(text: Optional[str]) -> str:
    """
    Loại bỏ hoàn toàn [FWD], nhãn tự động sinh ra khi forward trên Base.vn
    (Khách Quen: [EXTERNAL], Phân loại khách hàng, Tên Khách Hàng, Thông tin Chành xe/Chuyển phát...)
    và các trường custom field để trống (dạng 'Tên trường: ,').
    """
    if not text or str(text).strip().lower() in _EMPTY_NOTE_TOKENS:
        return ""

    cleaned = strip_forward_tags(text)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"^\s*Ghi chú:\s*", "", cleaned, flags=re.IGNORECASE)

    # 1. Xóa các cụm từ dư thừa đặc thù khi forward quy trình trên Base
    for pattern in _BOILERPLATE_FIELD_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    # 2. Xóa các nhãn custom field để trống giá trị (VD: "Tên trường: ," hoặc "Mã vận đơn: ·")
    cleaned = re.sub(
        r"(?:^|(?<=[,·•|\n]))\s*[^:,\n]{1,45}:\s*(?:no value|none|null)?\s*(?=[,·•|\n]|$)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    # 3. Chuẩn hóa dấu phân cách và dọn ký tự thừa còn sót lại
    cleaned = re.sub(r"\s*[·•|]+\s*", ", ", cleaned)
    cleaned = re.sub(r"\s*,\s*", ", ", cleaned)
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s+-\s+(?=,|$)", " ", cleaned)
    cleaned = re.sub(r"(?:^|,)\s*-\s+", ", ", cleaned)
    cleaned = re.sub(r"^[\s,·•|\-:]+|[\s,·•|\-:]+$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()

    if cleaned.lower() in _EMPTY_NOTE_TOKENS:
        return ""

    return cleaned


# =========================================================
# HÀM XỬ LÝ CHUẨN HÓA & TRÍCH XUẤT
# =========================================================

def generate_ticket_code() -> str:
    """Tạo mã phiếu ngẫu nhiên theo múi giờ Việt Nam: DTMMDD-XXXXX (VD: DT1009-47291)."""
    date_str = datetime.now(VN_TZ).strftime("%m%d")
    low = 10 ** (TICKET_SUFFIX_LENGTH - 1)
    high = (10 ** TICKET_SUFFIX_LENGTH) - 1

    suffix = random.randint(low, high)
    return f"{TICKET_PREFIX}{date_str}-{suffix}"


def clean_base_content(raw_text: str) -> str:
    """Làm sạch chuỗi gộp content_short của Base nếu không có custom field."""
    return strip_base_boilerplate(raw_text)


def normalize_phone(phone: Optional[str]) -> str:
    """Chuẩn hóa SĐT về dạng 0xxxxxxxxx (10 chữ số)."""
    if not phone:
        return ""
    digits = re.sub(r'\D', '', str(phone))

    if digits.startswith('84') and len(digits) == 11:
        digits = '0' + digits[2:]
    elif len(digits) == 9 and digits[0] in "35789":
        digits = '0' + digits

    return digits if len(digits) == 10 and digits.startswith('0') else ""


def extract_identifier_code_or_phone(text: str) -> Tuple[Optional[str], Optional[str]]:
    if not text:
        return None, None

    code_match = re.search(r'(?:NT|DT)-?\d{4}-\d{3,5}', text, re.IGNORECASE)
    code = code_match.group(0).upper() if code_match else None

    phone_match = PHONE_PATTERN.search(text)
    phone = normalize_phone(phone_match.group(0)) if phone_match else None

    return code, (phone or None)


def validate_status(raw_status: str, status_map: dict, metatype: str = "", state: str = "") -> str:
    """
    Chuẩn hóa & ánh xạ trạng thái chính xác theo thứ tự ưu tiên:
    1. Ánh xạ từ status_map động
    2. Kiểm tra metatype / state trực tiếp
    3. Tìm từ khóa trong tên stage
    4. Mặc định: "received"
    """
    normalized = (raw_status or "").strip().lower()

    if normalized in status_map:
        mapped = status_map[normalized]
        return "processing" if mapped == "repairing" else mapped

    if metatype == "done" or state == "done":
        return "fixed"
    if metatype in {"failed", "cancelled", "archived", "deleted"} or state in {"failed", "cancelled", "archived", "deleted"}:
        return "cancelled"

    if any(kw in normalized for kw in ["thông tin đơn", "tiếp nhận", "nhận máy", "tạo mới", "mới", "reception", "received"]):
        return "received"
    if any(kw in normalized for kw in ["hoàn thành", "sửa xong", "xong", "đã sửa", "done", "finished", "hoàn tất"]):
        return "fixed"
    if any(kw in normalized for kw in ["tiến hành", "sửa chữa", "đang sửa", "xử lý", "processing"]):
        return "processing"
    if any(kw in normalized for kw in ["trả máy", "trả", "bàn giao", "returned"]):
        return "returned"
    if any(kw in normalized for kw in ["hủy", "cancel", "failed", "deleted", "archive"]):
        return "cancelled"

    return "received"


def parse_task_name(task_name: str, base_note: str = "") -> Dict[str, str]:
    """
    Tách thông tin thông minh dựa trên mốc SĐT (Pivot) sau khi đã loại bỏ [FWD] & rác Base:
    - TRƯỚC SĐT -> Họ tên khách hàng
    - SĐT       -> Chuẩn hóa SĐT
    - SAU SĐT   -> Địa chỉ / Chành xe / Tỉnh thành
    """
    clean_base_note = strip_base_boilerplate(base_note)
    clean_task_name = strip_forward_tags(task_name)

    if not clean_task_name:
        return {
            "customer_name": "Khách lẻ",
            "phone": "",
            "address": "",
            "note": "Chưa ghi nhận",
            "received_note": clean_base_note,
        }

    phone = ""
    customer_name = "Khách lẻ"
    address = ""

    phone_match = PHONE_PATTERN.search(clean_task_name)

    if phone_match:
        raw_phone = phone_match.group(0)
        phone = normalize_phone(raw_phone)
        start_idx, end_idx = phone_match.span()

        left_part = clean_task_name[:start_idx].strip()
        left_part = re.sub(r'[\+\-\|\/,;\s]+$', '', left_part).strip()
        left_part = strip_forward_tags(left_part)
        if left_part:
            customer_name = left_part

        right_part = clean_task_name[end_idx:].strip()
        right_part = re.sub(r'^[\+\-\|\/,;\s]+', '', right_part).strip()
        right_part = strip_forward_tags(right_part)
        if right_part:
            address = right_part
    else:
        separators = r'[\+\-\|\/,;]+'
        segments = [
            strip_forward_tags(s)
            for s in re.split(separators, clean_task_name)
            if strip_forward_tags(s)
        ]
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
        "note": "Chưa ghi nhận",
        "received_note": clean_base_note,
    }


# =========================================================
# HÀM TRÍCH XUẤT & PHÂN LOẠI HÌNH ẢNH TỪ BASE.VN
# =========================================================

def _build_base_cdn_url(fid: str) -> str:
    """Dựng URL CDN công khai của Base.vn từ fid."""
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
    return (
        f"https://data-gcdn.basecdn.net/{p1}/{p2}/{p3}/{p4}/{p5}/{p6}/{p7}"
        f"/{dir_part}/{hash_dirs}/{md5_2}/{filename}"
    )


def _decode_base64_list(raw_val) -> list:
    if not raw_val:
        return []
    if isinstance(raw_val, list):
        return raw_val
    raw_b64 = str(raw_val).strip()
    if raw_b64.lower() in {"no value", "none", "null", ""}:
        return []
    if raw_b64.startswith("["):
        parsed = json.loads(raw_b64)
        return parsed if isinstance(parsed, list) else [parsed]
    if len(raw_b64) % 4:
        raw_b64 += "=" * (4 - len(raw_b64) % 4)
    parsed = json.loads(base64.b64decode(raw_b64).decode("utf-8"))
    return parsed if isinstance(parsed, list) else [parsed]


_IMG_RECEIVED_KEYWORDS = (
    "tiep_nhan", "nhan_may", "hien_trang", "ban_dau", "received", "checkin"
)
_IMG_REPAIR_KEYWORDS = (
    "sua_chua", "kiem_tra", "ky_thuat", "ktv", "loi", "linh_kien", "test", "repair"
)
_IMG_STORAGE_KEYWORDS = (
    "luu_kho", "kho", "hoa_don", "hang_da_soan", "dong_goi", "storage", "warehouse"
)
_IMG_DELIVERY_KEYWORDS = (
    "giao_nhan", "chanh_xe", "van_chuyen", "ship", "ban_giao", "tra_may", "delivery"
)


def _classify_image_bucket(
    field_name: str,
    workflow_id: str,
    workflow_settings: dict,
    stage_status: str = "",
    has_existing_received: bool = False,
) -> str:
    """
    Phân loại chính xác rổ chứa ảnh theo Quy trình + Giai đoạn + Tên trường:
    - Workflow Giao nhận -> 'delivery'
    - Workflow Lưu kho -> 'storage' (trừ field giao_nhan/chanh_xe -> 'delivery')
    - Workflow Sửa chữa:
        + Field kho/hóa đơn/hàng đã soạn -> 'storage'
        + Field kỹ thuật/sửa chữa/kiểm tra -> 'repair'
        + Field tiếp nhận/nhận máy/hiện trạng -> 'received'
        + Giai đoạn 'received' (tạo mới / tiếp nhận) -> 'received'
        + Giai đoạn 'returned' + field giao nhận/bàn giao -> 'delivery'
        + Các giai đoạn kiểm tra/sửa chữa ('processing', 'fixed', ...) -> 'repair'
    """
    fn = (field_name or "").lower()
    target_storage = str(workflow_settings.get("workflow_storage_id", "18720")).strip()
    target_delivery = str(workflow_settings.get("workflow_delivery_id", "18249")).strip()

    if workflow_id == target_delivery:
        return "delivery"

    if workflow_id == target_storage:
        if any(kw in fn for kw in _IMG_DELIVERY_KEYWORDS):
            return "delivery"
        return "storage"

    if any(kw in fn for kw in _IMG_STORAGE_KEYWORDS):
        return "storage"

    if any(kw in fn for kw in _IMG_REPAIR_KEYWORDS):
        return "repair"

    if any(kw in fn for kw in _IMG_RECEIVED_KEYWORDS):
        return "received"

    if stage_status == "received":
        return "received"

    if stage_status == "returned":
        if any(kw in fn for kw in _IMG_DELIVERY_KEYWORDS):
            return "delivery"
        return "repair"

    return "repair"


def _parse_maybe_json_dict(val) -> Optional[dict]:
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val.strip().startswith("{"):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def extract_all_base_images(
    payload: dict,
    workflow_id: str,
    workflow_settings: dict,
    stage_status: str = "",
    has_existing_received: bool = False,
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """
    Quét động các trường '-fids' (và fallback '-direct_links'), giải mã Base64
    và phân vào đúng 4 rổ: received / repair / storage / delivery.
    """
    sources = []
    if isinstance(payload, dict):
        sources.append(("root", payload))
        cf_root = _parse_maybe_json_dict(payload.get("custom_fields"))
        if cf_root:
            sources.append(("custom_fields", cf_root))
        job_dict = _parse_maybe_json_dict(payload.get("job"))
        if job_dict:
            sources.append(("job", job_dict))
            cf_job = _parse_maybe_json_dict(job_dict.get("custom_fields"))
            if cf_job:
                sources.append(("job.custom_fields", cf_job))

    by_field: Dict[str, List[str]] = {}
    img_groups: Dict[str, List[str]] = {
        "received": [],
        "repair": [],
        "delivery": [],
        "storage": [],
    }

    for src_name, src in sources:
        if not isinstance(src, dict):
            continue

        field_keys = set()
        for k in src.keys():
            ks = str(k)
            if ks.endswith("-fids"):
                field_keys.add(ks[:-5])
            elif ks.endswith("-direct_links"):
                field_keys.add(ks[:-13])

        for field_clean in field_keys:
            field_urls = []

            fids_raw = src.get(f"{field_clean}-fids")
            if fids_raw:
                try:
                    fids_list = _decode_base64_list(fids_raw)
                except Exception as e:
                    logger.error(f"❌ Lỗi parse danh sách -fids tại trường {field_clean} ({src_name}): {e}")
                    fids_list = []

                for fid in fids_list:
                    fid_str = str(fid).strip()
                    if not fid_str or fid_str.lower() in {"no value", "none", "null"}:
                        continue
                    try:
                        url = (
                            fid_str
                            if fid_str.startswith(("http://", "https://"))
                            else _build_base_cdn_url(fid_str)
                        )
                        field_urls.append(url)
                    except Exception as e:
                        logger.error(f"❌ Lỗi dựng CDN URL từ fid '{fid_str}' tại {field_clean}: {e}")

            if not field_urls:
                links_raw = src.get(f"{field_clean}-direct_links")
                if links_raw:
                    try:
                        links_list = _decode_base64_list(links_raw)
                    except Exception as e:
                        logger.error(f"❌ Lỗi parse danh sách -direct_links tại {field_clean} ({src_name}): {e}")
                        links_list = []

                    for link in links_list:
                        link_str = str(link).strip()
                        if link_str.startswith(("http://", "https://")):
                            field_urls.append(link_str)

            if field_urls:
                by_field[field_clean] = list(dict.fromkeys(by_field.get(field_clean, []) + field_urls))
                bucket = _classify_image_bucket(
                    field_clean,
                    workflow_id,
                    workflow_settings,
                    stage_status=stage_status,
                    has_existing_received=has_existing_received,
                )
                img_groups[bucket].extend(field_urls)

    for bucket in img_groups:
        img_groups[bucket] = list(dict.fromkeys(img_groups[bucket]))

    logger.info(
        f"🖼️ [IMAGES] WF={workflow_id} | stage={stage_status or 'n/a'} | "
        f"received={len(img_groups['received'])}, repair={len(img_groups['repair'])}, "
        f"storage={len(img_groups['storage'])}, delivery={len(img_groups['delivery'])}"
    )
    return img_groups, by_field