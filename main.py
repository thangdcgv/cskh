import logging
import os
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, status, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from routers.auth import AuthRequiredError, DatabaseConnectionError


from database import settings
from websocket_manager import manager
# BỔ SUNG: Import router 'sync' đã viết ở bước trước
from routers import reception, webhook, tickets, auth, sync, settings as settings_router
from routers.auth import extract_user_from_session, SUPER_ADMIN_ROLES

logger = logging.getLogger("uvicorn.error")

# === Tự động khởi tạo thư mục tài nguyên tĩnh ===
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
ICONS_DIR = STATIC_DIR / "icons"
SOUNDS_DIR = STATIC_DIR / "sounds"

# Tự tạo thư mục nếu chưa có để tránh lỗi RuntimeError
ICONS_DIR.mkdir(parents=True, exist_ok=True)
SOUNDS_DIR.mkdir(parents=True, exist_ok=True)


# === Cấu hình Lifespan (Chạy khi ứng dụng khởi động & tắt) ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 [SYSTEM] Hệ thống Quản lý Sửa chữa & Máy in - Đại Thành đã khởi động thành công!")
    yield
    logger.info("🛑 [SYSTEM] Hệ thống đang dừng...")

def _is_html_request(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept or "text/html" in request.headers.get("content-type", "")

# === Khởi tạo Ứng dụng ===
app = FastAPI(
    title="Quản lý Sửa chữa & Máy In - Đại Thành",
    description="Hệ thống quản lý tiếp nhận, theo dõi và cấu hình sửa chữa",
    version="1.0.0",
    lifespan=lifespan
)

# === Cấu hình CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Middleware Session Bảo mật ===
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="session_id",
    max_age=30 * 24 * 60 * 60,  # 30 ngày
    https_only=(os.getenv("ENV", "dev") == "prod"),
    same_site="lax"
)

# === Mount Static Files & Templates ===
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory="templates")

# === Đăng ký Các Router Nghiệp Vụ ===
app.include_router(auth.router)
app.include_router(reception.router)
app.include_router(webhook.router)
app.include_router(tickets.router)
app.include_router(sync.router)            # BỔ SUNG: API đối soát làm sạch dữ liệu
app.include_router(settings_router.router)


# === Fix lỗi 404 Service Worker & Favicon ===
@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Xử lý request /sw.js từ trình duyệt để tránh lỗi 404 trong Terminal"""
    sw_file = STATIC_DIR / "sw.js"
    if sw_file.exists():
        return FileResponse(sw_file, media_type="application/javascript")
    return Response(content="// Service Worker Placeholder", media_type="application/javascript")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Xử lý request /favicon.ico từ trình duyệt"""
    icon_file = ICONS_DIR / "favicon.ico"
    if icon_file.exists():
        return FileResponse(icon_file)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# === Health Check Endpoint ===
@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok", "app": "repair-management"}


# === Giao diện Trang chủ / Tiếp nhận ===
@app.get("/", response_class=HTMLResponse)
async def home_dashboard(request: Request):
    user = extract_user_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="reception.html",
        context={
            "current_user": user,
            "supabase_url": getattr(settings, "SUPABASE_URL", ""),
            "supabase_anon_key": getattr(settings, "SUPABASE_ANON_KEY", getattr(settings, "SUPABASE_KEY", ""))
        }
    )


# === Giao diện Tra cứu / Theo dõi ===
@app.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request):
    user = extract_user_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="tracking.html",
        context={
            "current_user": user,
            "supabase_url": getattr(settings, "SUPABASE_URL", ""),
            "supabase_anon_key": getattr(settings, "SUPABASE_ANON_KEY", getattr(settings, "SUPABASE_KEY", ""))
        }
    )


# === Cài đặt Hệ thống ===
@app.get("/settings", response_class=HTMLResponse)
@app.get("/setting", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = extract_user_from_session(request)

    # 1. Kiểm tra đăng nhập
    if not user:
        logger.warning("Truy cập /settings thất bại: Không tìm thấy Session người dùng")
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # 2. Chuẩn hóa chuỗi Role
    role = str(user.get("role", "")).strip().lower().replace("_", " ")
    admin_roles = {"admin", "super admin", "system admin", "systemadmin"}.union(SUPER_ADMIN_ROLES)

    # 3. Phân quyền Admin
    if role not in admin_roles:
        logger.warning(f"Người dùng {user.get('username')} (Role: {role}) không có quyền truy cập /settings")
        return RedirectResponse(url="/?error=unauthorized", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_user": user,
            "user_role": role,
            "supabase_url": getattr(settings, "SUPABASE_URL", ""),
            "supabase_anon_key": getattr(settings, "SUPABASE_ANON_KEY", getattr(settings, "SUPABASE_KEY", ""))
        }
    )


@app.get("/admin")
async def redirect_admin_to_settings():
    return RedirectResponse(url="/settings", status_code=status.HTTP_301_MOVED_PERMANENTLY)


# === Realtime WebSocket Endpoint ===
@app.websocket("/ws/tickets")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("Client ngắt kết nối WebSocket bình thường")
    except Exception as e:
        logger.error(f"Lỗi WebSocket: {e}")
    finally:
        await manager.disconnect(websocket)

# 1. Bắt lỗi chưa đăng nhập → HTML thì redirect, API thì 401
@app.exception_handler(AuthRequiredError)
async def auth_required_handler(request: Request, exc: AuthRequiredError):
    request.session.clear()
    if _is_html_request(request):
        return RedirectResponse(
            url=f"/login?error={exc.reason}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return JSONResponse(
        status_code=401,
        content={"detail": "Phiên làm việc đã hết hạn, vui lòng đăng nhập lại"},
    )


# 2. Bắt lỗi mất kết nối DB → xóa session + về login
@app.exception_handler(DatabaseConnectionError)
async def db_error_handler(request: Request, exc: DatabaseConnectionError):
    logger.error(f"DATABASE CONNECTION LOST: {exc}")
    try:
        request.session.clear()
    except Exception:
        pass
    if _is_html_request(request):
        return RedirectResponse(url="/login?error=db_error", status_code=303)
    return JSONResponse(status_code=503, content={"detail": "Hệ thống tạm thời gián đoạn"})


# 3. Bắt MỌI lỗi 500 còn lại → HTML thì về login, API thì 500 JSON
@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logger.error(f"UNHANDLED ERROR on {request.url.path}: {exc}", exc_info=True)
    # Nếu là lỗi DB/network → xóa session luôn
    err_msg = str(exc).lower()
    is_db_error = any(k in err_msg for k in ["connection", "timeout", "supabase", "postgrest", "database", "network"])
    if is_db_error:
        try:
            request.session.clear()
        except Exception:
            pass
    if _is_html_request(request):
        reason = "db_error" if is_db_error else "server_error"
        return RedirectResponse(url=f"/login?error={reason}", status_code=303)
    return JSONResponse(status_code=500, content={"detail": "Có lỗi xảy ra, vui lòng thử lại"})
# === Custom Exception Handlers (Tránh hiển thị JSON lỗi thô ráp) ===
@app.exception_handler(404)
async def custom_404_handler(request: Request, exc):
    """Tự động chuyển hướng về trang chủ khi người dùng gõ sai URL"""
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)