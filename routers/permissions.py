import logging
import time
from typing import Any, Dict, List, Set, Union

from fastapi import APIRouter, Header, HTTPException
from starlette.concurrency import run_in_threadpool

from database import settings, supabase_admin

logger = logging.getLogger(__name__)

router = APIRouter()

# Cấu hình cấp bậc Role
ROLE_LEVELS: Dict[str, int] = {
    "user": 1,
    "admin": 2,
    "super admin": 3,
    "system admin": 4,
}

# Bộ nhớ đệm RAM
PERMISSIONS_CACHE: Dict[str, Any] = {}
IS_CACHE_LOADED: bool = False
LAST_CACHE_UPDATE: float = 0

STAFF_DEPT_CACHE: Dict[str, str] = {}
LAST_STAFF_CACHE_UPDATE: float = 0

CACHE_TTL_SECONDS: int = 300  # 5 phút TTL


def get_user_departments(user: dict) -> Set[str]:
    """Lấy tập hợp mã phòng ban chuẩn (IN HOA) của user từ session hoặc user_metadata."""
    if not user:
        return set()
    meta = user.get("user_metadata") or {}
    raw = (
        user.get("departments")
        or user.get("department")
        or user.get("phong_ban")
        or meta.get("departments")
        or meta.get("department")
        or ""
    )
    if isinstance(raw, (list, tuple, set)):
        return {str(d).strip().upper() for d in raw if str(d).strip()}
    return {d.strip().upper() for d in str(raw).split(",") if d.strip()}


def load_permissions_cache(force: bool = False) -> None:
    """Nạp ma trận phân quyền từ CSDL vào RAM Cache."""
    global PERMISSIONS_CACHE, IS_CACHE_LOADED, LAST_CACHE_UPDATE

    now = time.time()
    if IS_CACHE_LOADED and not force and (now - LAST_CACHE_UPDATE < CACHE_TTL_SECONDS):
        return

    try:
        res = (
            supabase_admin.table("page_permissions")
            .select("page_key, allowed_roles, allowed_departments")
            .execute()
        )
        new_cache = {}

        if res.data:
            for item in res.data:
                page_key = str(item.get("page_key") or "").strip()
                if not page_key:
                    continue

                roles = {
                    str(r).strip().lower().replace("_", " ")
                    for r in (item.get("allowed_roles") or [])
                }
                depts = {
                    str(d).strip().upper()
                    for d in (item.get("allowed_departments") or ["ALL"])
                }

                new_cache[page_key] = {
                    "roles": roles,
                    "departments": depts,
                }

        PERMISSIONS_CACHE = new_cache
        IS_CACHE_LOADED = True
        LAST_CACHE_UPDATE = now
        logger.info(f"✅ Đã tải {len(PERMISSIONS_CACHE)} trang phân quyền vào Cache RAM.")
    except Exception as e:
        logger.error(f"❌ Lỗi nạp permissions cache: {e}")


def get_staff_department_map(force: bool = False) -> Dict[str, str]:
    """Lấy danh sách ánh xạ từ tên nhân viên/username sang phòng ban (Có Cache RAM 5 phút)."""
    global STAFF_DEPT_CACHE, LAST_STAFF_CACHE_UPDATE

    now = time.time()
    if STAFF_DEPT_CACHE and not force and (now - LAST_STAFF_CACHE_UPDATE < CACHE_TTL_SECONDS):
        return STAFF_DEPT_CACHE

    try:
        res = (
            supabase_admin.table("quan_tri_vien")
            .select("ho_ten, username, department")
            .execute()
        )
        mapping = {}
        if res.data:
            for row in res.data:
                dept = str(row.get("department") or "").strip().lower()
                ho_ten = str(row.get("ho_ten") or "").strip().lower()
                username = str(row.get("username") or "").strip().lower()

                if ho_ten:
                    mapping[ho_ten] = dept
                if username:
                    mapping[username] = dept

        STAFF_DEPT_CACHE = mapping
        LAST_STAFF_CACHE_UPDATE = now
        return STAFF_DEPT_CACHE
    except Exception as e:
        logger.error(f"❌ Lỗi lấy mapping phòng ban nhân viên: {e}")
        return STAFF_DEPT_CACHE or {}


def can_access(arg1: Union[str, dict], arg2: Union[str, dict]) -> bool:
    """
    Kiểm tra quyền truy cập trang/chức năng qua Cache RAM (RBAC + Data Scope).
    Hỗ trợ cả 2 cách gọi: can_access(user, page_key) hoặc can_access(page_key, user).
    """
    if isinstance(arg1, dict):
        user = arg1
        page_key = str(arg2 or "").strip()
    elif isinstance(arg2, dict):
        user = arg2
        page_key = str(arg1 or "").strip()
    else:
        return False

    if not user or not page_key:
        return False

    user_role = str(user.get("role") or "").strip().lower().replace("_", " ")
    user_depts = get_user_departments(user)
    user_level = ROLE_LEVELS.get(user_role, 1)

    # 1. System Admin luôn có toàn quyền
    if user_role == "system admin" or user_level >= ROLE_LEVELS["system admin"]:
        return True

    # 2. Nạp hoặc tự động làm mới Cache khi hết hạn TTL
    load_permissions_cache()

    perm = PERMISSIONS_CACHE.get(page_key)
    if not perm:
        return False

    if isinstance(perm, set):
        allowed_roles = perm
        allowed_depts = {"ALL"}
    elif isinstance(perm, dict):
        allowed_roles = perm.get("roles", set())
        allowed_depts = perm.get("departments", {"ALL"})
    else:
        return False

    # 3. Kiểm tra Phạm vi Phòng Ban (Super Admin bỏ qua giới hạn phòng ban)
    if user_role != "super admin" and "ALL" not in allowed_depts:
        if not user_depts or not (user_depts & allowed_depts):
            return False

    # 4. Kiểm tra Vai trò được tick trong bảng phân quyền
    return user_role in allowed_roles


def get_allowed_workflows(user: dict) -> List[str]:
    """Trả về danh sách tab quy trình được phép hiển thị/thao tác theo phòng ban."""
    if not user:
        return []

    user_role = str(user.get("role") or "").strip().lower().replace("_", " ")
    if ROLE_LEVELS.get(user_role, 1) >= ROLE_LEVELS["super admin"]:
        return ["repair", "storage", "delivery", "installation", "all"]

    depts = get_user_departments(user)
    if not depts or "ALL" in depts:
        return ["repair", "storage", "delivery", "installation", "all"]

    dept_workflows = {
        "KTSC": ["repair", "storage"],
        "KTLD": ["installation"],
        "GN": ["delivery", "storage"],
        "CSKH": ["repair", "storage", "delivery", "installation", "all"],
        "KD": ["installation", "delivery", "all"],
        "KT": ["storage", "all"],
    }

    ordered_tabs = ["repair", "storage", "delivery", "installation", "all"]
    allowed_set: Set[str] = set()
    for d in depts:
        allowed_set.update(dept_workflows.get(d, []))

    if not allowed_set:
        return ["repair"]

    return [tab for tab in ordered_tabs if tab in allowed_set]


def can_modify_warranty_record(user: dict, record: dict) -> bool:
    """
    Kiểm tra quyền Sửa/Xóa phiếu bảo hành:
    - Super Admin / System Admin: Toàn quyền.
    - Tác giả tạo phiếu: Được phép.
    - Người cùng phòng ban với tác giả: Được phép.
    """
    if not user or not record:
        return False

    role = str(user.get("role") or "").strip().lower().replace("_", " ")
    if role in ["super admin", "system admin"]:
        return True

    user_identifiers = {
        str(user.get("ho_ten") or "").strip().lower(),
        str(user.get("display_name") or "").strip().lower(),
        str(user.get("username") or "").strip().lower(),
        str(user.get("email") or "").strip().lower(),
    }
    user_identifiers.discard("")

    creator = str(record.get("created_by") or record.get("staff_name") or "").strip().lower()
    if creator and creator in user_identifiers:
        return True

    user_depts = {d.lower() for d in get_user_departments(user)}
    if user_depts:
        staff_dept_map = get_staff_department_map()
        record_dept = staff_dept_map.get(creator, "")
        if record_dept and record_dept in user_depts:
            return True

    return False


def parse_int(val, default_val: int = 0) -> int:
    """Parse giá trị sang int an toàn, trả về default_val nếu lỗi hoặc rỗng."""
    try:
        if val is None or str(val).strip() == "":
            return default_val
        return int(val)
    except (ValueError, TypeError):
        return default_val


# --------------------------------------------------------------------------
# WEBHOOK NỘI BỘ: NHẬN TÍN HIỆU LÀM MỚI CACHE TỪ CENTER HUB
# --------------------------------------------------------------------------
@router.post("/api/v1/internal/reload-permissions", include_in_schema=False)
async def reload_permissions_webhook(x_internal_secret: str = Header(default="")):
    if not x_internal_secret or x_internal_secret != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal secret")

    await run_in_threadpool(lambda: load_permissions_cache(force=True))
    await run_in_threadpool(lambda: get_staff_department_map(force=True))
    return {"success": True, "message": "Đã làm mới RAM Cache phân quyền & phòng ban"}
