import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from fastapi import APIRouter, Request, Form, status, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from database import supabase, supabase_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["Authentication"])

# =========================================================
# CẤU HÌNH HẰNG SỐ CHUYÊN CHO HỆ THỐNG SỬA CHỮA (REPAIR)
# =========================================================
APP_CODE = os.getenv("APP_CODE", "REPAIR")  # Mã phân định ứng dụng Tiếp nhận & Sửa chữa
SUPER_ADMIN_ROLES = {"super admin", "system admin"}
ALLOWED_ROLES = {"user", "admin", "super admin", "system admin"}

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
# HELPER FUNCTIONS
# =========================================================
def render_template(
    request: Request,
    name: str,
    context: Optional[dict] = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
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
    """Xác minh phiên làm việc còn hiệu lực trong DB với APP_CODE = 'REPAIR'"""
    def _check():
        res = supabase_admin.table("user_sessions").select("id") \
            .eq("user_id", user_id).eq("app_code", APP_CODE) \
            .eq("session_token", session_token).limit(1).execute()
        return bool(res and res.data)

    try:
        return await run_in_threadpool(_check)
    except Exception as e:
        logger.error(f"VERIFY SESSION DB ERROR: {e}")
        return False  # Fail-closed: DB lỗi -> Từ chối kết nối


async def require_login(request: Request) -> Dict[str, Any]:
    """Dependency: Lấy thông tin user hiện tại hoặc bắn lỗi 401 nếu hết hạn"""
    user = extract_user_from_session(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Chưa đăng nhập hoặc phiên làm việc đã hết hạn"
        )
    if not await verify_active_session(user["auth_id"], user["session_token"]):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Phiên làm việc đã bị đăng xuất từ thiết bị khác"
        )
    return user


get_current_user = require_login


async def get_current_user_or_redirect(request: Request) -> Optional[Dict[str, Any]]:
    """Lấy user hiện tại, nếu không hợp lệ thì dọn dẹp session và trả về None"""
    try:
        return await require_login(request)
    except HTTPException:
        return None


def get_redirect_url_by_role(role: str) -> str:
    """Định tuyến sau khi đăng nhập: Điều hướng về Màn hình Lễ tân Tiếp nhận (/) cho tất cả vai trò"""
    return "/"


# =========================================================
# 1. ĐĂNG NHẬP
# =========================================================
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = request.session.get("user_id")
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    # Sửa cú pháp TemplateResponse dùng kwargs rõ ràng
    return templates.TemplateResponse(request=request, name="login.html")


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    email_or_user = email.strip()
    if not email_or_user or not password:
        return render_template(request, "login.html", {"error": "Vui lòng nhập đầy đủ thông tin."}, status_code=400)

    # Xử lý đăng nhập bằng username hoặc email
    login_email = email_or_user
    if "@" not in login_email:
        login_email = f"{login_email}@gmail.com"

    try:
        auth_res = await run_in_threadpool(
            supabase.auth.sign_in_with_password,
            {"email": login_email, "password": password}
        )
        if not auth_res or not auth_res.user:
            raise ValueError("Không tìm thấy thông tin xác thực người dùng")

        auth_id = str(auth_res.user.id)

        # Truy vấn thông tin người dùng từ DB
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

        # Thu thập thông tin client
        xff = request.headers.get("x-forwarded-for")
        client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "Unknown")
        user_agent = (request.headers.get("user-agent", "Unknown"))[:255]
        device_type = detect_device_type(user_agent)
        new_session_token = str(uuid.uuid4())

        # Đồng bộ session vào DB (Xóa session cũ của cùng thiết bị + APP_CODE)
        def _sync_session():
            supabase_admin.table("user_sessions").delete() \
                .eq("user_id", auth_id).eq("app_code", APP_CODE) \
                .eq("device_type", device_type).execute()

            supabase_admin.table("user_sessions").insert({
                "user_id": auth_id,
                "app_code": APP_CODE,
                "session_token": new_session_token,
                "ip_address": client_ip,
                "user_agent": user_agent,
                "device_type": device_type,
            }).execute()

        await run_in_threadpool(_sync_session)

        # Cập nhật thông tin vào Cookie Session
        request.session.clear()
        request.session.update({
            "user_id": auth_id,
            "session_token": new_session_token,
            "user_email": auth_res.user.email or login_email,
            "username": username,
            "ho_ten": ho_ten,
            "role": role,
            "department": department,
            "device_type": device_type,
            "access_token": auth_res.session.access_token if auth_res.session else None,
        })

        return RedirectResponse(url=get_redirect_url_by_role(role), status_code=status.HTTP_303_SEE_OTHER)

    except Exception as e:
        logger.warning(f"LOGIN FAILED cho tài khoản '{email_or_user}': {e}")
        return render_template(
            request, "login.html",
            {"error": "Tài khoản hoặc mật khẩu không chính xác."},
            status_code=401
        )

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
async def change_password_page(request: Request):
    if not await get_current_user_or_redirect(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return render_template(request, "change_password.html", {"error": None, "success": None})


@router.post("/change-password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    user = await get_current_user_or_redirect(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    valid, err = is_valid_password(new_password)
    if not valid:
        return render_template(request, "change_password.html", {"error": err, "success": None}, status_code=400)

    try:
        await run_in_threadpool(
            supabase.auth.sign_in_with_password,
            {"email": user["email"], "password": current_password}
        )
        await run_in_threadpool(
            supabase_admin.auth.admin.update_user_by_id,
            user["auth_id"],
            {"password": new_password},
        )
        return render_template(request, "change_password.html", {"error": None, "success": "Đổi mật khẩu thành công!"})
    except Exception as e:
        logger.error(f"CHANGE PASSWORD ERROR: {e}")
        return render_template(
            request, "change_password.html",
            {"error": "Mật khẩu hiện tại không đúng hoặc hệ thống gặp lỗi.", "success": None},
            status_code=400
        )


# =========================================================
# 4. QUÊN & ĐẶT LẠI MẬT KHẨU
# =========================================================
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return render_template(request, "forgot_password.html", {"message": None, "error": None})


@router.post("/forgot-password")
async def forgot_password_submit(request: Request, email: str = Form(...)):
    email_clean = email.strip().lower()
    msg = "Nếu email tồn tại trong hệ thống, hướng dẫn khôi phục đã được gửi."
    try:
        base_url = str(request.base_url).rstrip("/")
        await run_in_threadpool(
            supabase.auth.reset_password_for_email,
            email_clean,
            options={"redirect_to": f"{base_url}/update-password"},
        )
    except Exception as e:
        logger.warning(f"FORGOT PASSWORD ERROR: {e}")
    return render_template(request, "forgot_password.html", {"message": msg, "error": None})


@router.get("/update-password", response_class=HTMLResponse)
async def update_password_page(request: Request):
    access_token = request.query_params.get("access_token")
    return render_template(request, "update_password.html", {
        "error": None, "success": None, "access_token": access_token
    })


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
        return render_template(request, "update_password.html", {"error": "Thiếu mã xác thực (Access Token).", "success": None}, status_code=400)

    try:
        user_res = await run_in_threadpool(supabase_admin.auth.get_user, token)
        if not user_res or not user_res.user:
            raise ValueError("Token không hợp lệ hoặc đã hết hạn")

        await run_in_threadpool(
            supabase_admin.auth.admin.update_user_by_id,
            str(user_res.user.id),
            {"password": new_password},
        )
        return render_template(request, "update_password.html", {"error": None, "success": "Đặt lại mật khẩu thành công!"})
    except Exception as e:
        logger.error(f"UPDATE PASSWORD ERROR: {e}")
        return render_template(
            request, "update_password.html",
            {"error": "Link không hợp lệ hoặc đã hết hạn.", "success": None},
            status_code=400
        )