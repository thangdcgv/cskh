import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from fastapi import APIRouter, Request, Form, status, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from database import supabase, supabase_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["Authentication"])

APP_CODE = os.getenv("APP_CODE", "REPAIR")
SUPER_ADMIN_ROLES = {"super admin", "system admin"}

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = (
    BASE_DIR.parent / "templates"
    if (BASE_DIR.parent / "templates").exists()
    else BASE_DIR / "templates"
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEVICE_TYPE_DESKTOP = "desktop"
DEVICE_TYPE_MOBILE = "mobile"


# =========================================================
# EXCEPTION TÙY CHỈNH
# =========================================================
class AuthRequiredError(Exception):
    def __init__(self, reason: str = "unauthenticated"):
        self.reason = reason
        super().__init__(reason)


class DatabaseConnectionError(Exception):
    pass


# =========================================================
# HELPERS
# =========================================================
def render_template(request, name, context=None, status_code=200):
    return templates.TemplateResponse(
        request=request, name=name, context=context or {}, status_code=status_code
    )


def is_valid_password(password: str) -> Tuple[bool, Optional[str]]:
    if not password:
        return False, "Mật khẩu không được để trống."
    if len(password) < 8:
        return False, "Mật khẩu phải có ít nhất 8 ký tự."
    return True, None


def detect_device_type(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    mobile_keywords = ["android", "iphone", "ipad", "ipod", "mobile", "webos", "blackberry", "windows phone", "tablet"]
    return DEVICE_TYPE_MOBILE if any(k in ua for k in mobile_keywords) else DEVICE_TYPE_DESKTOP


def extract_user_from_session(request: Request) -> Optional[Dict[str, Any]]:
    user_id = request.session.get("user_id")
    session_token = request.session.get("session_token")
    if not user_id or not session_token:
        return None
    role = str(request.session.get("role", "user")).strip().lower()
    dept = request.session.get("department") or "KTSC"
    if role in SUPER_ADMIN_ROLES:
        dept = "ALL"
    return {
        "auth_id": str(user_id),
        "id": str(user_id),
        "email": request.session.get("user_email", ""),
        "username": request.session.get("username", ""),
        "ho_ten": request.session.get("ho_ten", "Quản trị viên"),
        "name": request.session.get("ho_ten", "Quản trị viên"),
        "role": role,
        "department": dept,
        "access_token": request.session.get("access_token"),
        "session_token": session_token,
        "device_type": request.session.get("device_type"),
    }


async def verify_active_session(user_id: str, session_token: str) -> bool:
    def _check():
        res = supabase_admin.table("user_sessions").select("id") \
            .eq("user_id", user_id).eq("app_code", APP_CODE) \
            .eq("session_token", session_token).limit(1).execute()
        return bool(res and res.data)
    try:
        return await run_in_threadpool(_check)
    except Exception as e:
        logger.error(f"DB CONNECTION ERROR khi verify session: {e}")
        raise DatabaseConnectionError(str(e))


async def require_login(request: Request) -> Dict[str, Any]:
    user = extract_user_from_session(request)
    if not user:
        raise AuthRequiredError("no_session")
    try:
        valid = await verify_active_session(user["auth_id"], user["session_token"])
    except DatabaseConnectionError:
        request.session.clear()
        raise
    if not valid:
        request.session.clear()
        raise AuthRequiredError("session_expired")
    return user


get_current_user = require_login


async def get_current_user_or_redirect(request: Request) -> Dict[str, Any]:
    user = extract_user_from_session(request)
    if not user:
        raise AuthRequiredError("no_session")
    try:
        valid = await verify_active_session(user["auth_id"], user["session_token"])
    except DatabaseConnectionError:
        request.session.clear()
        raise
    if not valid:
        request.session.clear()
        raise AuthRequiredError("session_expired")
    return user


def _is_auth_error(e: Exception) -> bool:
    """Phân biệt lỗi xác thực thật vs lỗi hệ thống/DB."""
    msg = str(e).lower()
    auth_keywords = [
        "invalid login", "invalid credentials", "email not confirmed",
        "user not found", "invalid password", "wrong password",
        "email not found", "user already registered",
    ]
    return any(k in msg for k in auth_keywords)


# =========================================================
# 1. ĐĂNG NHẬP
# =========================================================
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    error = request.query_params.get("error")
    error_msg = {
        "session_expired": "Phiên làm việc đã hết hạn, vui lòng đăng nhập lại.",
        "db_error": "Hệ thống tạm thời gián đoạn, vui lòng đăng nhập lại.",
        "server_error": "Có lỗi xảy ra, vui lòng thử lại.",
    }.get(error)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": error_msg}
    )


@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email_or_user = email.strip()
    if not email_or_user or not password:
        return render_template(request, "login.html", {"error": "Vui lòng nhập đầy đủ thông tin."}, status_code=400)

    login_email = email_or_user if "@" in email_or_user else f"{email_or_user}@gmail.com"

    # BƯỚC 1: Xác thực Supabase — tách riêng để phân biệt lỗi
    try:
        auth_res = await run_in_threadpool(
            supabase.auth.sign_in_with_password,
            {"email": login_email, "password": password}
        )
    except Exception as e:
        if _is_auth_error(e):
            logger.info(f"LOGIN FAILED (sai thông tin) '{email_or_user}': {e}")
            return render_template(request, "login.html", {"error": "Tài khoản hoặc mật khẩu không chính xác."}, status_code=401)
        # Lỗi hệ thống/DB — KHÔNG báo sai mật khẩu
        logger.error(f"LOGIN SYSTEM ERROR (Supabase auth): {e}", exc_info=True)
        return render_template(request, "login.html", {"error": "Hệ thống tạm thời gián đoạn, vui lòng thử lại sau."}, status_code=503)

    if not auth_res or not auth_res.user:
        return render_template(request, "login.html", {"error": "Tài khoản hoặc mật khẩu không chính xác."}, status_code=401)

    auth_id = str(auth_res.user.id)

    # BƯỚC 2: Lấy profile & đồng bộ session — lỗi ở đây KHÔNG phải sai mật khẩu
    try:
        def _fetch_profile():
            return supabase_admin.table("quan_tri_vien").select("*") \
                .eq("auth_id", auth_id).limit(1).execute()
        profile_res = await run_in_threadpool(_fetch_profile)

        username = email_or_user.split("@")[0]
        ho_ten = "Quản trị viên"
        role = "user"
        department = "KTSC"
        if profile_res and profile_res.data:
            info = profile_res.data[0]
            username = info.get("username") or username
            ho_ten = info.get("ho_ten") or info.get("name") or ho_ten
            role = str(info.get("role", "user")).strip().lower()
            department = "ALL" if role in SUPER_ADMIN_ROLES else str(info.get("department") or "KTSC").strip()

        xff = request.headers.get("x-forwarded-for")
        client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "Unknown")
        user_agent = (request.headers.get("user-agent", "Unknown"))[:255]
        device_type = detect_device_type(user_agent)
        new_session_token = str(uuid.uuid4())

        def _sync_session():
            supabase_admin.table("user_sessions").delete() \
                .eq("user_id", auth_id).eq("app_code", APP_CODE) \
                .eq("device_type", device_type).execute()
            supabase_admin.table("user_sessions").insert({
                "user_id": auth_id, "app_code": APP_CODE,
                "session_token": new_session_token,
                "ip_address": client_ip, "user_agent": user_agent,
                "device_type": device_type,
            }).execute()
        await run_in_threadpool(_sync_session)

    except Exception as e:
        # Lỗi DB/ hệ thống sau khi xác thực thành công — KHÔNG báo sai mật khẩu
        logger.error(f"LOGIN SYSTEM ERROR (profile/session): {e}", exc_info=True)
        return render_template(request, "login.html", {"error": "Hệ thống tạm thời gián đoạn, vui lòng thử lại sau."}, status_code=503)

    # BƯỚC 3: Lưu session — an toàn
    request.session.clear()
    request.session.update({
        "user_id": auth_id, "session_token": new_session_token,
        "user_email": auth_res.user.email or login_email,
        "username": username, "ho_ten": ho_ten,
        "role": role, "department": department,
        "device_type": device_type,
        "access_token": auth_res.session.access_token if auth_res.session else None,
    })
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================
# 2. ĐĂNG XUẤT
# =========================================================
@router.get("/logout")
async def logout(request: Request):
    user_id = request.session.get("user_id")
    session_token = request.session.get("session_token")
    if user_id and session_token:
        def _remove():
            supabase_admin.table("user_sessions").delete() \
                .eq("user_id", user_id).eq("app_code", APP_CODE) \
                .eq("session_token", session_token).execute()
        try:
            await run_in_threadpool(_remove)
        except Exception as e:
            logger.error(f"LOGOUT ERROR: {e}")
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


# =========================================================
# 3. ĐỔI MẬT KHẨU
# =========================================================
@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user=Depends(get_current_user_or_redirect)):
    return render_template(request, "change_password.html", {"error": None, "success": None})


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    user=Depends(get_current_user_or_redirect),
):
    valid, err = is_valid_password(new_password)
    if not valid:
        return render_template(request, "change_password.html", {"error": err, "success": None}, status_code=400)
    try:
        # ✅ Sửa: truyền dict
        await run_in_threadpool(
            supabase.auth.sign_in_with_password,
            {"email": user["email"], "password": current_password}
        )
        await run_in_threadpool(
            supabase_admin.auth.admin.update_user_by_id,
            user["auth_id"], {"password": new_password},
        )
        return render_template(request, "change_password.html", {"error": None, "success": "Đổi mật khẩu thành công!"})
    except Exception as e:
        if _is_auth_error(e):
            return render_template(request, "change_password.html", {"error": "Mật khẩu hiện tại không đúng.", "success": None}, status_code=400)
        logger.error(f"CHANGE PASSWORD SYSTEM ERROR: {e}", exc_info=True)
        return render_template(request, "change_password.html", {"error": "Hệ thống tạm thời gián đoạn.", "success": None}, status_code=503)


# =========================================================
# 4. QUÊN & ĐẶT LẠI MẬT KHẨU
# =========================================================
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return render_template(request, "forgot_password.html", {"message": None, "error": None})


@router.post("/forgot-password")
async def forgot_password_submit(request: Request, email: str = Form(...)):
    email_clean = email.strip().lower()
    msg = "Nếu email tồn tại, hướng dẫn khôi phục đã được gửi."
    try:
        base_url = str(request.base_url).rstrip("/")
        # ✅ Sửa: truyền positional args
        await run_in_threadpool(
            supabase.auth.reset_password_for_email,
            email_clean,
            {"redirect_to": f"{base_url}/update-password"}
        )
    except Exception as e:
        logger.warning(f"FORGOT PASSWORD: {e}")
    return render_template(request, "forgot_password.html", {"message": msg, "error": None})


@router.get("/update-password", response_class=HTMLResponse)
async def update_password_page(request: Request):
    access_token = request.query_params.get("access_token")
    return render_template(request, "update_password.html", {"error": None, "success": None, "access_token": access_token})


@router.post("/update-password")
async def update_password_submit(
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    access_token: Optional[str] = Form(None),
):
    if new_password != confirm_password:
        return render_template(request, "update_password.html", {"error": "Xác nhận mật khẩu không khớp.", "success": None}, status_code=400)
    valid, err = is_valid_password(new_password)
    if not valid:
        return render_template(request, "update_password.html", {"error": err, "success": None}, status_code=400)
    token = access_token or request.query_params.get("access_token")
    if not token:
        return render_template(request, "update_password.html", {"error": "Thiếu mã xác thực.", "success": None}, status_code=400)
    try:
        user_res = await run_in_threadpool(supabase_admin.auth.get_user, token)
        if not user_res or not user_res.user:
            raise ValueError("Token không hợp lệ")
        await run_in_threadpool(
            supabase_admin.auth.admin.update_user_by_id,
            str(user_res.user.id), {"password": new_password},
        )
        return render_template(request, "update_password.html", {"error": None, "success": "Đặt lại mật khẩu thành công!"})
    except Exception as e:
        logger.error(f"UPDATE PASSWORD ERROR: {e}")
        return render_template(request, "update_password.html", {"error": "Link không hợp lệ hoặc đã hết hạn.", "success": None}, status_code=400)