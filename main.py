import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from database import settings
from websocket_manager import manager
from routers import reception, webhook,tickets, auth, settings as settings_router
from routers.auth import extract_user_from_session, SUPER_ADMIN_ROLES

logger = logging.getLogger("uvicorn.error")

# === Tự động khởi tạo thư mục tài nguyên tĩnh ===
STATIC_DIR = Path("static")
ICONS_DIR = STATIC_DIR / "icons"
ICONS_DIR.mkdir(parents=True, exist_ok=True)

# === Khởi tạo Ứng dụng ===
app = FastAPI(
    title="Quản lý Sửa chữa & Máy In - Đại Thành",
    description="Hệ thống quản lý tiếp nhận, theo dõi và cấu hình sửa chữa",
    version="1.0.0"
)

# === Cấu hình CORS (Cho phép gọi API / Webhook an toàn) ===
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
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# === Đăng ký Các Router Nghiệp Vụ ===
app.include_router(auth.router)
app.include_router(reception.router)
app.include_router(webhook.router)
app.include_router(tickets.router)
app.include_router(settings_router.router)


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
        context={"current_user": user}
    )


# === Giao diện Tra cứu / Theo dõi ===
@app.get("/tracking", response_class=HTMLResponse)
async def tracking_page(request: Request):
    user = extract_user_from_session(request)
    return templates.TemplateResponse(
        request=request,
        name="tracking.html",
        context={"current_user": user}
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

    # 2. Chuẩn hóa chuỗi Role (loại bỏ gạch dưới và khoảng trắng)
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
            "user_role": role
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