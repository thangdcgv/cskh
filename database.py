import os
import sys
import logging
from pathlib import Path
from dotenv import load_dotenv
from supabase import create_client, Client, ClientOptions

logger = logging.getLogger("uvicorn.error")

# === Tải biến môi trường ===
BASE_DIR = Path(__file__).resolve().parent
ENV_PATHS = [
    BASE_DIR / ".env",
    BASE_DIR.parent / ".env",
    Path(".env"),  # Thư mục hiện tại
]

for env_path in ENV_PATHS:
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
        logger.info(f"Đã tải cấu hình từ: {env_path}")
        break
else:
    logger.warning("Không tìm thấy file .env — dùng biến hệ thống")


class Settings:
    """Cấu hình với validation chặt chẽ"""

    # Supabase
    SUPABASE_URL: str = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    SUPABASE_KEY: str = os.getenv("SUPABASE_KEY", "").strip()
    SUPABASE_SERVICE_ROLE_KEY: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    # Bảo mật
    SECRET_KEY: str = os.getenv("SECRET_KEY", "").strip()

    # Tùy chỉnh kết nối
    DB_TIMEOUT: int = int(os.getenv("DB_TIMEOUT", "15"))
    MAX_RETRIES: int = int(os.getenv("DB_MAX_RETRIES", "3"))

    def validate(self):
        """Kiểm tra cấu hình bắt buộc"""
        errors = []
        if not self.SUPABASE_URL:
            errors.append("SUPABASE_URL chưa cấu hình")
        if not self.SUPABASE_KEY:
            errors.append("SUPABASE_KEY chưa cấu hình")
        if not self.SECRET_KEY or len(self.SECRET_KEY) < 16:
            errors.append("SECRET_KEY quá ngắn / chưa cấu hình")
        if errors:
            raise RuntimeError("Cấu hình thiếu: " + "; ".join(errors))

    @property
    def has_admin_privilege(self) -> bool:
        return bool(self.SUPABASE_SERVICE_ROLE_KEY)


settings = Settings()

# === Tùy chọn kết nối ===
BASE_OPTIONS = ClientOptions(
    postgrest_client_timeout=settings.DB_TIMEOUT,
    auto_refresh_token=True,   # ✅ Tự làm mới token
    persist_session=False,    # Không lưu phiên ra đĩa
)


def create_anon_client() -> Client:
    """Tạo client khóa công khai — dùng cho hầu hết yêu cầu"""
    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_KEY,
        options=BASE_OPTIONS,
    )


def create_admin_client() -> Client:
    """Tạo client quyền quản trị — chỉ dùng khi cần"""
    if not settings.SUPABASE_SERVICE_ROLE_KEY:
        logger.warning(
            "SUPABASE_SERVICE_ROLE_KEY chưa cấu hình — bỏ qua chức năng quản trị"
        )
        return create_anon_client()

    return create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_SERVICE_ROLE_KEY,
        options=BASE_OPTIONS,
    )


# === Khởi tạo & xác thực cấu hình ===
try:
    settings.validate()
except RuntimeError as e:
    logger.critical(f"Thiếu cấu hình: {e}")
    sys.exit(1)

# Tạo client — có thể tạo lại nếu cần
supabase: Client = create_anon_client()
supabase_admin: Client = create_admin_client()

# === Xuất cấu hình cho kiểm tra ===
__all__ = [
    "supabase",
    "supabase_admin",
    "settings",
    "create_anon_client",
    "create_admin_client",
]