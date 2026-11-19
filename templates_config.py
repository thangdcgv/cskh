from pathlib import Path
from fastapi.templating import Jinja2Templates

from database import settings
from routers.permissions import can_access

# 1. Tự động xác định đường dẫn thư mục templates an toàn
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = (
    BASE_DIR / "templates"
    if (BASE_DIR / "templates").exists()
    else BASE_DIR.parent / "templates"
)

# 2. Khởi tạo Jinja2Templates duy nhất cho toàn ứng dụng
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 3. Trích xuất thông số Supabase từ object settings (hỗ trợ fallback an toàn)
SUPABASE_URL = getattr(settings, "SUPABASE_URL", "")
SUPABASE_KEY = getattr(
    settings, "SUPABASE_ANON_KEY", getattr(settings, "SUPABASE_KEY", "")
)

# 4. Đăng ký các biến & hàm global dùng chung cho Jinja2
templates.env.globals["SUPABASE_URL"] = SUPABASE_URL
templates.env.globals["SUPABASE_KEY"] = SUPABASE_KEY
templates.env.globals["can_access"] = can_access