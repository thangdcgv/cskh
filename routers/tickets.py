import logging
import os
import re
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from database import supabase, supabase_admin
from websocket_manager import manager, VN_TZ
from routers.auth import get_current_user, get_current_user_or_redirect

router = APIRouter(tags=["Tickets Queue"])

# === KHAI BÁO THƯ MỤC TEMPLATES ===
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = (
    BASE_DIR.parent / "templates"
    if (BASE_DIR.parent / "templates").exists()
    else BASE_DIR / "templates"
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger("uvicorn.error")

# === ÁNH XẠ PHÒNG BAN & CHUẨN HÓA ===
DEPT_PREFIX_MAP = {
    "KTSC": "SC",
    "KTLD": "LD",
    "KD": "KD",
    "CSKH": "CS",
    "KT": "KT",
    "GN": "GN",
    "ALL": "ALL"
}

DEPT_NAME_MAP = {
    "KTSC": "Phòng Sửa Chữa",
    "KTLD": "Phòng Lắp Đặt",
    "KD": "Phòng Kinh Doanh",
    "CSKH": "Phòng CSKH",
    "KT": "Phòng Kế Toán",
    "GN": "Phòng Giao Nhận",
    "ALL": "Toàn Công Ty"
}

def normalize_dept_to_ticket(dept: Optional[str]) -> str:
    if not dept:
        return "KTSC"
    d = str(dept).strip().upper()
    return d if d in DEPT_PREFIX_MAP else "KTSC"

def normalize_role(raw_role: Optional[str]) -> str:
    r = str(raw_role or "").strip().lower()
    if r in ("system admin", "super admin"):
        return "super admin"
    if r in ("admin", "quản lý", "quan ly"):
        return "admin"
    if r in ("ktv", "ktsc", "kỹ thuật", "ky thuat"):
        return "ktv"
    if r in ("kd", "kinh doanh", "kinhdoanh", "sales"):
        return "kd"
    return "ktv"

DepartmentType = Literal["KTSC", "KTLD", "KD", "CSKH", "KT", "GN", "ALL"]
StatusType = Literal["waiting", "calling", "processing", "completed", "cancelled", "skipped"]

# === PYDANTIC SCHEMAS ===
class CreateTicketRequest(BaseModel):
    department: DepartmentType
    customer_name: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=8)
    device_info: Optional[str] = None

class UpdateStatusRequest(BaseModel):
    status: StatusType
    assigned_to: Optional[str] = None

class CallTicketRequest(BaseModel):
    assigned_to: Optional[str] = None

class TransferDepartmentRequest(BaseModel):
    new_department: DepartmentType
    assigned_to: Optional[str] = None
    device_info: Optional[str] = None

# === HELPER FUNCTIONS ===
def check_supabase():
    if supabase is None or supabase_admin is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Chưa cấu hình SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY!"
        )

def get_today_utc_start() -> str:
    now_vn = datetime.now(VN_TZ)
    today_vn_start = now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
    return today_vn_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

async def generate_ticket_code(department: str) -> str:
    check_supabase()
    prefix = DEPT_PREFIX_MAP.get(department, "ST")
    today_start = get_today_utc_start()
    try:
        query = lambda: supabase_admin.table("tickets") \
            .select("ticket_code") \
            .eq("department", department) \
            .gte("created_at", today_start) \
            .order("id", desc=True) \
            .limit(1) \
            .execute()
        res = await asyncio.to_thread(query)
        next_number = 1
        if res.data and len(res.data) > 0:
            last_code = res.data[0].get("ticket_code", "")
            match = re.search(r'(\d+)$', last_code)
            if match:
                next_number = int(match.group(1)) + 1
        return f"{prefix}-{next_number:03d}"
    except Exception as e:
        logger.error(f"Lỗi sinh mã ticket: {e}")
        return f"{prefix}-001"

async def safe_broadcast(message: dict) -> None:
    try:
        await manager.broadcast(message)
    except Exception as e:
        logger.warning(f"WebSocket broadcast failed: {e}")


# ==========================================
# === 🔌 WEBSOCKET ENDPOINT ===
# ==========================================
@router.websocket("/ws/tickets")
@router.websocket("/api/ws/tickets")
async def websocket_tickets_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "connected"})
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"Lỗi WebSocket: {e}")
        manager.disconnect(websocket)


# ==========================================
# === 🖥 GIAO DIỆN HTML ===
# ==========================================
@router.get("/ticket", response_class=HTMLResponse)
@router.get("/tickets", response_class=HTMLResponse)
async def public_ticket_page(request: Request):
    """Giao diện bốc số điện tử dành cho khách hàng."""
    return templates.TemplateResponse(
        request=request,
        name="public_ticket.html",
        context={"request": request}
    )

@router.get("/admin/queue", response_class=HTMLResponse)
@router.get("/api/admin-queue", response_class=HTMLResponse)
async def admin_queue_page(
    request: Request,
    user: dict = Depends(get_current_user_or_redirect)
):
    """Giao diện Quản lý hàng chờ cho nhân viên."""
    check_supabase()

    user_id = user.get("id") or user.get("auth_id")
    raw_role = user.get("role", "user")
    role_clean = normalize_role(raw_role)

    user_dept = "KTSC"
    user_name = user.get("ho_ten") or user.get("full_name") or "Nhân viên"

    try:
        query = lambda: supabase_admin.table("quan_tri_vien") \
            .select("department, ho_ten") \
            .eq("auth_id", str(user_id)) \
            .limit(1) \
            .execute()
        res = await asyncio.to_thread(query)

        if res.data and len(res.data) > 0:
            user_data = res.data[0]
            if user_data.get("ho_ten"):
                user_name = user_data.get("ho_ten")

            raw_dept = user_data.get("department")
            if raw_dept:
                d = str(raw_dept).strip().upper()
                if role_clean in ["admin", "super admin", "system admin"]:
                    user_dept = d if d in DEPT_PREFIX_MAP else "ALL"
                else:
                    user_dept = d if d in DEPT_PREFIX_MAP else "KTSC"
    except Exception as e:
        logger.error(f"Lỗi lấy thông tin phòng ban người dùng {user_id}: {e}")

    current_user = {
        "name": user_name,
        "ho_ten": user_name,
        "role": role_clean,
        "department": user_dept
    }

    return templates.TemplateResponse(
        request=request,
        name="admin_queue.html",
        context={"request": request, "current_user": current_user,
                 "supabase_url": os.getenv("SUPABASE_URL", ""),
                 "supabase_key": os.getenv("SUPABASE_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")},
        
    )


# ==========================================
# === 🎫 API BỐC SỐ & HÀNG CHỜ ===
# ==========================================
@router.post("/api/tickets/create")
@router.post("/tickets/create")
async def create_ticket(data: CreateTicketRequest):
    check_supabase()
    try:
        phone_clean = data.phone.strip() if data.phone and data.phone.strip() else None
        customer_name_clean = data.customer_name.strip() if data.customer_name else "Khách hàng"
        device_info_clean = data.device_info.strip() if data.device_info and data.device_info.strip() else None

        if not phone_clean:
            raise HTTPException(status_code=400, detail="Vui lòng cung cấp SĐT !")

        today_start = get_today_utc_start()
        query_check = lambda: supabase_admin.table("tickets") \
            .select("*") \
            .gte("created_at", today_start) \
            .eq("phone", phone_clean) \
            .in_("status", ["waiting", "calling", "processing"]) \
            .execute()

        existing_ticket = await asyncio.to_thread(query_check)
        if existing_ticket.data and len(existing_ticket.data) > 0:
            return {
                "success": False,
                "message": "Bạn đã có một phiếu đang chờ xử lý!",
                "ticket": existing_ticket.data[0]
            }

        ticket_code = await generate_ticket_code(data.department)
        payload = {
            "ticket_code": ticket_code,
            "department": data.department,
            "customer_name": customer_name_clean,
            "phone": phone_clean,
            "device_info": device_info_clean,
            "status": "waiting"
        }

        insert_query = lambda: supabase_admin.table("tickets").insert(payload).execute()
        insert_res = await asyncio.to_thread(insert_query)

        if insert_res.data and len(insert_res.data) > 0:
            new_ticket = insert_res.data[0]

            await safe_broadcast({
                "type": "new_ticket",
                "ticket": new_ticket,
                "department": new_ticket["department"],
                "timestamp": datetime.now(VN_TZ).isoformat()
            })
            logger.info(f"📢 Đã tạo vé: {ticket_code}")

            return {"success": True, "message": "Bốc số thành công!", "ticket": new_ticket}

        raise HTTPException(status_code=500, detail="Không thể lưu vé vào CSDL.")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lỗi bốc số: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Lỗi hệ thống: {str(e)}")


@router.get("/api/tickets/my-ticket")
@router.get("/tickets/my-ticket")
async def get_my_ticket(
    phone: Optional[str] = None,
    ticket_id: Optional[str] = None
):
    """Tra cứu phiếu hiện tại của khách hàng."""
    check_supabase()
    today_start = get_today_utc_start()

    query_fn = lambda: (
        supabase_admin.table("tickets")
        .select("*")
        .gte("created_at", today_start)
        .in_("status", ["waiting", "calling", "processing", "completed"])
        .eq("id", int(ticket_id)) if ticket_id and str(ticket_id).isdigit()
        else supabase_admin.table("tickets").select("*").gte("created_at", today_start).in_("status", ["waiting", "calling", "processing", "completed"]).eq("ticket_code", str(ticket_id).strip().upper()) if ticket_id
        else supabase_admin.table("tickets").select("*").gte("created_at", today_start).in_("status", ["waiting", "calling", "processing"])
    ).order("created_at", desc=True).limit(1).execute()

    res = await asyncio.to_thread(query_fn)

    if res.data and len(res.data) > 0:
        ticket = res.data[0]

        curr_query = lambda: supabase_admin.table("tickets") \
            .select("ticket_code") \
            .eq("department", ticket["department"]) \
            .in_("status", ["calling", "processing"]) \
            .gte("created_at", today_start) \
            .order("updated_at", desc=True) \
            .limit(1) \
            .execute()
        current_res = await asyncio.to_thread(curr_query)

        current_number = "--"
        if current_res.data and len(current_res.data) > 0:
            current_number = current_res.data[0].get("ticket_code", "--")
        ticket["current_number"] = current_number
        
        technician_name = None
        if ticket.get("assigned_to"):
            try:
                assigned_val = str(ticket["assigned_to"])
                ktv_query = lambda: supabase_admin.table("quan_tri_vien") \
                    .select("ho_ten") \
                    .or_(f"auth_id.eq.{assigned_val},username.eq.{assigned_val}") \
                    .limit(1).execute()
                ktv_res = await asyncio.to_thread(ktv_query)
                if ktv_res.data and len(ktv_res.data) > 0:
                    technician_name = ktv_res.data[0].get("ho_ten")
            except Exception:
                technician_name = ticket.get("assigned_to")
        ticket["technician_name"] = technician_name
        return {"has_ticket": True, "ticket": ticket}

    return {"has_ticket": False, "ticket": None}


@router.get("/api/tickets/queue")
@router.get("/tickets/queue")
async def get_queue_list(
    department: Optional[str] = Query(None, description="KTSC/KTLD/KD/CSKH/KT/GN/ALL"),
    status_filter: Optional[str] = Query(None, alias="status", description="waiting/calling/processing/completed/cancelled")
):
    check_supabase()
    try:
        today_start = get_today_utc_start()

        def fetch_data():
            q = supabase_admin.table("tickets").select("*").or_(
                f"status.in.(waiting,calling,processing),created_at.gte.{today_start}"
            )
            if department and department != "ALL":
                q = q.eq("department", department)
            if status_filter and status_filter != "all":
                q = q.eq("status", status_filter)
            return q.order("id", desc=False).execute()

        res = await asyncio.to_thread(fetch_data)
        tickets = res.data or []

        stats = {
            "waiting": sum(1 for t in tickets if t.get("status") in ["waiting", "calling"]),
            "processing": sum(1 for t in tickets if t.get("status") == "processing"),
            "completed": sum(1 for t in tickets if t.get("status") == "completed")
        }

        return {"success": True, "total": len(tickets), "stats": stats, "tickets": tickets}
    except Exception as e:
        logger.error(f"Lỗi hàng chờ: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tickets/call/{ticket_id}")
@router.post("/tickets/call/{ticket_id}")
async def call_ticket_endpoint(
    ticket_id: int, 
    data: Optional[CallTicketRequest] = None,
    user: dict = Depends(get_current_user)
):
    check_supabase()
    try:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "status": "processing",
            "called_at": now_utc,
            "updated_at": now_utc
        }
        if data and data.assigned_to:
            payload["assigned_to"] = data.assigned_to
        else:
            payload["assigned_to"] = user.get("ho_ten") or user.get("username") or str(user.get("id"))

        query_update = lambda: supabase_admin.table("tickets").update(payload).eq("id", ticket_id).execute()
        res = await asyncio.to_thread(query_update)

        if res.data and len(res.data) > 0:
            updated_ticket = res.data[0]
            
            await safe_broadcast({
                "type": "status_update",
                "ticket": updated_ticket,
                "status": "processing",
                "timestamp": datetime.now(VN_TZ).isoformat()
            })
            
            logger.info(f"📢 Đã gọi/tiếp nhận vé ID {ticket_id}")
            return {"success": True, "message": "Đã tiếp nhận vé!", "ticket": updated_ticket}
            
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu yêu cầu.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lỗi gọi số vé: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/tickets/{ticket_id}/status")
@router.patch("/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: int, 
    data: UpdateStatusRequest,
    user: dict = Depends(get_current_user)
):
    check_supabase()
    try:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "status": data.status,
            "updated_at": now_utc
        }
        if data.status in ["calling", "processing"]:
            payload["called_at"] = now_utc

        if data.assigned_to:
            payload["assigned_to"] = data.assigned_to

        query_update = lambda: supabase_admin.table("tickets").update(payload).eq("id", ticket_id).execute()
        res = await asyncio.to_thread(query_update)

        if res.data:
            updated_ticket = res.data[0]
            await safe_broadcast({
                "type": "status_update",
                "ticket": updated_ticket,
                "status": data.status,
                "timestamp": datetime.now(VN_TZ).isoformat()
            })
            return {"success": True, "message": "Cập nhật thành công!", "ticket": updated_ticket}
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/tickets/{ticket_id}/transfer")
@router.patch("/tickets/{ticket_id}/transfer")
async def transfer_ticket_department(
    ticket_id: int, 
    data: TransferDepartmentRequest,
    user: dict = Depends(get_current_user)
):
    check_supabase()
    try:
        query_check = lambda: supabase_admin.table("tickets").select("status, ticket_code").eq("id", ticket_id).limit(1).execute()
        existing = await asyncio.to_thread(query_check)

        if not existing.data:
            raise HTTPException(status_code=404, detail="Không tìm thấy phiếu yêu cầu.")
        
        current_ticket = existing.data[0]
        current_status = current_ticket.get("status")
        
        if current_status in ["cancelled", "completed"]:
            status_labels = {
                "cancelled": "đã bị hủy",
                "completed": "đã hoàn thành"
            }
            raise HTTPException(
                status_code=400, 
                detail=f"Phiếu này {status_labels.get(current_status, current_status)}, không thể chuyển phòng!"
            )

        new_code = await generate_ticket_code(data.new_department)
        payload = {
            "department": data.new_department,
            "ticket_code": new_code,
            "status": "waiting",
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        if data.assigned_to:
            payload["assigned_to"] = data.assigned_to
        if data.device_info is not None:
            payload["device_info"] = data.device_info.strip()

        query_update = lambda: supabase_admin.table("tickets").update(payload).eq("id", ticket_id).execute()
        res = await asyncio.to_thread(query_update)
        
        if res.data:
            updated_ticket = res.data[0]
            
            await safe_broadcast({
                "type": "transfer_ticket",
                "ticket": updated_ticket,
                "new_department": data.new_department,
                "timestamp": datetime.now(VN_TZ).isoformat()
            })
            
            logger.info(f"📢 Chuyển phòng thành công: Vé cũ {current_ticket.get('ticket_code')} → Mã mới {new_code} sang phòng {data.new_department}")
            
            return {
                "success": True,
                "message": f"Đã chuyển vé sang phòng {data.new_department.upper()} với mã mới {new_code}",
                "ticket": updated_ticket
            }
            
        raise HTTPException(status_code=404, detail="Không tìm thấy phiếu hoặc cập nhật thất bại.")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lỗi hệ thống khi chuyển phòng vé {ticket_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))