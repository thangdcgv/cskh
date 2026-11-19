import logging
import time
from typing import Any, Dict, Union

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
    Kiểm tra quyền truy cập trang qua Cache RAM (RBAC + Data Scope).
    Hỗ trợ linh hoạt cả 2 vị trí truyền tham số:
    - can_access(user, page_key)
    - can_access(page_key, user)
    """
    if isinstance(arg1, dict):
        user = arg1
        page_key = str(arg2 or "").strip()
    elif isinstance(arg2, dict):
        user = arg2
        page_key = str(arg1 or "").strip()
    else:
        return False

    if not user:
        return False

    user_role = str(user.get("role") or "").strip().lower().replace("_", " ")
    user_dept = str(user.get("department") or "KTSC").strip().upper()
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

    # 3. Kiểm tra Phạm vi Phòng Ban (Department Scope)
    if user_role != "super admin" and "ALL" not in allowed_depts:
        if user_dept not in allowed_depts:
            return False

    # 4. Kiểm tra Cấp bậc Vai trò (Role Level)
    if user_role in allowed_roles:
        return True

    allowed_levels = [ROLE_LEVELS.get(r, 1) for r in allowed_roles if r in ROLE_LEVELS]
    if allowed_levels and user_level >= min(allowed_levels):
        return True

    return False


def can_modify_warranty_record(user: dict, record: dict) -> bool:
    """
    Kiểm tra quyền Sửa/Xóa phiếu bảo hành:
    - Super Admin / System Admin: Toàn quyền.
    - Tác giả tạo phiếu: Được phép (So sánh trùng với ho_ten, username hoặc email).
    - Người cùng phòng ban với tác giả: Được phép.
    """
    if not user or not record:
        return False

    role = str(user.get("role") or "").strip().lower().replace("_", " ")
    if role in ["super admin", "system admin"]:
        return True

    # 1. Gom tất cả danh xưng định danh của User hiện tại
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

    # 2. Kiểm tra cùng phòng ban
    user_dept = str(user.get("department") or "").strip().lower()
    if not user_dept:
        meta = user.get("user_metadata", {})
        user_dept = str(meta.get("department") or "").strip().lower()

    if user_dept:
        staff_dept_map = get_staff_department_map()
        record_dept = staff_dept_map.get(creator, "")

        if record_dept and user_dept == record_dept:
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