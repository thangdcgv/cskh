import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from database import supabase_admin
from routers.auth import get_current_user
from routers.permissions import (
    can_access,
    can_modify_warranty_record,
    get_staff_department_map,
)
from schemas import (
    CleanedPrinterCreate,
    CleanedPrinterMoveRequest,
    CleanedPrinterResponse,
    CleanedPrinterStatusUpdate,
    CleanedPrinterUpdate,
)
from templates_config import templates

logger = logging.getLogger("uvicorn.error")

router = APIRouter(
    prefix="/api/v1/cleaned-printers",
    tags=["Cleaned Printers Inventory"]
)


# --------------------------------------------------------------------------
# HELPERS & PHÂN QUYỀN CENTER HUB
# --------------------------------------------------------------------------
def sanitize_search_query(query_str: str) -> str:
    """Escapes SQL/PostgREST wildcard characters to prevent search exploitation."""
    if not query_str:
        return ""
    return re.sub(r"([\%\_\\])", r"\\\1", query_str.strip())


def normalize_dept_code(raw_dept: Optional[str]) -> str:
    """Chuẩn hóa mã phòng ban về dạng chuẩn (GN, KTDM, KTSC, KD...)."""
    d = str(raw_dept or "").strip().upper()
    if d in {"GN", "GIAO NHẬN", "GIAO NHAN", "PHÒNG GIAO NHẬN", "PHONG GIAO NHAN"}:
        return "GN"
    if d in {"KTDM", "KỸ THUẬT DỌN MÁY", "KY THUAT DON MAY", "DỌN MÁY", "DON MAY"}:
        return "KTDM"
    return d or "KTSC"


async def require_cleaned_printers_access(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Kiểm tra quyền truy cập phân hệ Kho Máy Dọn theo ma trận Center Hub."""
    allowed = await run_in_threadpool(can_access, "cleaned_printers", current_user)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền truy cập chức năng Quản lý kho máy dọn.",
        )
    return current_user


def _format_printer_row(row: dict, staff_dept_map: dict) -> dict:
    """Chuẩn hóa dữ liệu trả về và tự động ánh xạ phòng ban của KTV."""
    creator = str(row.get("created_by") or "").strip()
    raw_dept = row.get("department") or staff_dept_map.get(creator.lower()) or "KTSC"
    dept = normalize_dept_code(raw_dept)

    return {
        "id": row["id"],
        "model_name": row["model_name"],
        "classification": row["phan_loai"],
        "quantity": row["so_luong"],
        "status": row["trang_thai"],
        "ghi_chu": row.get("ghi_chu"),
        "department": dept,
        "created_by": creator,
        "updated_by": row.get("updated_by"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_note(note: Optional[str]) -> Optional[str]:
    if not note:
        return None
    cleaned = note.strip()
    return cleaned if cleaned else None


def _find_matching_printer_row(
    model_name: str,
    classification: str,
    status_val: str,
    note: Optional[str],
    created_by: str,
    exclude_id: Optional[int] = None,
) -> Optional[dict]:
    """Tìm bản ghi tồn kho trùng khớp để cộng dồn số lượng."""
    query = (
        supabase_admin.table("may_don_ton_kho")
        .select("*")
        .ilike("model_name", model_name.strip())
        .eq("phan_loai", classification)
        .eq("trang_thai", status_val)
        .eq("created_by", created_by)
    )
    if exclude_id is not None:
        query = query.neq("id", exclude_id)

    res = query.order("created_at", desc=False).execute()
    target_note = _normalize_note(note)

    for row in (res.data or []):
        row_note = _normalize_note(row.get("ghi_chu"))
        if row_note == target_note:
            return row
    return None


# --------------------------------------------------------------------------
# 0. GIAO DIỆN TEMPLATE HTML
# --------------------------------------------------------------------------
@router.get("/page", response_class=HTMLResponse)
async def cleaned_printers_page(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Render trang giao diện quản lý kho máy dọn (kiểm tra phân quyền động)."""
    allowed = await run_in_threadpool(can_access, "cleaned_printers", current_user)
    if not allowed:
        return HTMLResponse(
            "⛔ Bạn không có quyền truy cập trang Quản lý kho máy dọn!",
            status_code=status.HTTP_403_FORBIDDEN,
        )

    return templates.TemplateResponse(
        request=request,
        name="cleaned_printers.html",
        context={
            "request": request,
            "current_user": current_user,
        },
    )


# --------------------------------------------------------------------------
# 1. GET LIST OF CLEANED PRINTERS (WITH FILTERS & SEARCH)
# --------------------------------------------------------------------------
@router.get("", response_model=List[CleanedPrinterResponse])
async def get_cleaned_printers(
    status_filter: Optional[str] = Query(None, alias="status"),
    classification: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Search by printer model"),
    current_user: dict = Depends(require_cleaned_printers_access),
):
    try:
        clean_search = sanitize_search_query(search) if search else None

        def fetch_data():
            query = supabase_admin.table("may_don_ton_kho").select("*")
            if status_filter and status_filter.strip():
                query = query.eq("trang_thai", status_filter.strip())
            if classification and classification.strip():
                query = query.eq("phan_loai", classification.strip())
            if clean_search:
                query = query.ilike("model_name", f"%{clean_search}%")
            return query.order("created_at", desc=True).execute()

        res = await run_in_threadpool(fetch_data)
        staff_dept_map = await run_in_threadpool(get_staff_department_map)

        return [_format_printer_row(row, staff_dept_map) for row in (res.data or [])]

    except Exception as e:
        logger.error(f"❌ Lỗi truy vấn danh sách máy dọn: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Không thể tải danh sách máy dọn. Vui lòng thử lại sau!",
        )


# --------------------------------------------------------------------------
# 2. GET PRINTER MODELS LIST (FOR DROPDOWN FROM list_printer TABLE)
# --------------------------------------------------------------------------
@router.get("/models", response_model=List[dict])
async def get_printer_model_options(
    current_user: dict = Depends(require_cleaned_printers_access),
):
    try:
        res = await run_in_threadpool(
            lambda: supabase_admin.table("list_printer")
            .select("id, brand_name, model_code, category")
            .order("brand_name")
            .execute()
        )

        models = []
        for p in (res.data or []):
            full_name = f"{(p.get('brand_name') or '').upper()} {(p.get('model_code') or '')}".strip()
            models.append({
                "id": p["id"],
                "full_name": full_name,
                "category": p.get("category"),
            })
        return models

    except Exception as e:
        logger.error(f"❌ Lỗi lấy danh mục model máy in: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Không thể tải danh mục máy in.",
        )


# --------------------------------------------------------------------------
# 3. GET SINGLE CLEANED PRINTER BY ID
# --------------------------------------------------------------------------
@router.get("/{printer_id}", response_model=CleanedPrinterResponse)
async def get_cleaned_printer_by_id(
    printer_id: int,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    try:
        res = await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .select("*")
            .eq("id", printer_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy bản ghi máy dọn #{printer_id}",
            )

        staff_dept_map = await run_in_threadpool(get_staff_department_map)
        return _format_printer_row(res.data[0], staff_dept_map)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi lấy chi tiết máy dọn #{printer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi tải thông tin chi tiết.",
        )


# --------------------------------------------------------------------------
# 4. CREATE NEW CLEANED PRINTER RECORD (TỰ ĐỘNG CỘNG DỒN NẾU TRÙNG)
# --------------------------------------------------------------------------
@router.post("", response_model=CleanedPrinterResponse, status_code=status.HTTP_201_CREATED)
async def create_cleaned_printer(
    payload: CleanedPrinterCreate,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    username = current_user.get("ho_ten") or current_user.get("username") or "System"
    clean_model_name = payload.model_name.strip()
    if not clean_model_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tên model máy in không được để trống!",
        )

    add_qty = max(1, payload.quantity)
    clean_note = _normalize_note(payload.ghi_chu)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        def upsert_printer():
            existing = _find_matching_printer_row(
                model_name=clean_model_name,
                classification=payload.classification,
                status_val=payload.status,
                note=clean_note,
                created_by=username,
            )
            if existing:
                new_qty = int(existing["so_luong"]) + add_qty
                return (
                    supabase_admin.table("may_don_ton_kho")
                    .update({
                        "so_luong": new_qty,
                        "updated_by": username,
                        "updated_at": now_iso,
                    })
                    .eq("id", existing["id"])
                    .execute()
                )

            db_payload = {
                "model_name": clean_model_name,
                "phan_loai": payload.classification,
                "so_luong": add_qty,
                "trang_thai": payload.status,
                "ghi_chu": clean_note,
                "created_by": username,
                "updated_by": username,
            }
            return supabase_admin.table("may_don_ton_kho").insert(db_payload).execute()

        res = await run_in_threadpool(upsert_printer)
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Không thể lưu bản ghi máy dọn!",
            )

        staff_dept_map = await run_in_threadpool(get_staff_department_map)
        return _format_printer_row(res.data[0], staff_dept_map)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi tạo/cộng dồn bản ghi máy dọn: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi lưu bản ghi máy dọn.",
        )


# --------------------------------------------------------------------------
# 4B. MOVE / TRANSFER STOCK (LẤY MÁY & HOÀN KHO CÓ CỘNG DỒN TỰ ĐỘNG)
# --------------------------------------------------------------------------
@router.post("/{printer_id}/move", response_model=CleanedPrinterResponse)
async def move_cleaned_printer_stock(
    printer_id: int,
    payload: CleanedPrinterMoveRequest,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    username = current_user.get("ho_ten") or current_user.get("username") or "System"
    move_qty = max(1, payload.quantity)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        def process_move():
            src_res = (
                supabase_admin.table("may_don_ton_kho")
                .select("*")
                .eq("id", printer_id)
                .limit(1)
                .execute()
            )
            if not src_res.data:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Không tìm thấy bản ghi #{printer_id}!",
                )

            src = src_res.data[0]
            current_qty = int(src["so_luong"])
            if move_qty > current_qty:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Số lượng chuyển ({move_qty}) vượt quá tồn kho hiện tại ({current_qty})!",
                )

            if payload.clear_note:
                final_note = None
            elif payload.ghi_chu is not None:
                final_note = _normalize_note(payload.ghi_chu)
            else:
                final_note = _normalize_note(src.get("ghi_chu"))

            existing_target = _find_matching_printer_row(
                model_name=src["model_name"],
                classification=src["phan_loai"],
                status_val=payload.target_status,
                note=final_note,
                created_by=src["created_by"],
                exclude_id=printer_id,
            )

            if existing_target:
                target_res = (
                    supabase_admin.table("may_don_ton_kho")
                    .update({
                        "so_luong": int(existing_target["so_luong"]) + move_qty,
                        "updated_by": username,
                        "updated_at": now_iso,
                    })
                    .eq("id", existing_target["id"])
                    .execute()
                )
            elif move_qty == current_qty:
                return (
                    supabase_admin.table("may_don_ton_kho")
                    .update({
                        "trang_thai": payload.target_status,
                        "ghi_chu": final_note,
                        "updated_by": username,
                        "updated_at": now_iso,
                    })
                    .eq("id", printer_id)
                    .execute()
                )
            else:
                target_res = (
                    supabase_admin.table("may_don_ton_kho")
                    .insert({
                        "model_name": src["model_name"],
                        "phan_loai": src["phan_loai"],
                        "so_luong": move_qty,
                        "trang_thai": payload.target_status,
                        "ghi_chu": final_note,
                        "created_by": src["created_by"],
                        "updated_by": username,
                    })
                    .execute()
                )

            if move_qty < current_qty:
                supabase_admin.table("may_don_ton_kho").update({
                    "so_luong": current_qty - move_qty,
                    "updated_by": username,
                    "updated_at": now_iso,
                }).eq("id", printer_id).execute()
            else:
                supabase_admin.table("may_don_ton_kho").delete().eq("id", printer_id).execute()

            return target_res

        res = await run_in_threadpool(process_move)
        staff_dept_map = await run_in_threadpool(get_staff_department_map)
        return _format_printer_row(res.data[0], staff_dept_map)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi điều chuyển tồn kho #{printer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi điều chuyển số lượng máy.",
        )


# --------------------------------------------------------------------------
# 5. UPDATE CLEANED PRINTER RECORD
# --------------------------------------------------------------------------
@router.put("/{printer_id}", response_model=CleanedPrinterResponse)
async def update_cleaned_printer(
    printer_id: int,
    payload: CleanedPrinterUpdate,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    username = current_user.get("ho_ten") or current_user.get("username") or "System"
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))

    update_data = {}
    if payload.model_name is not None:
        clean_name = payload.model_name.strip()
        if not clean_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tên model máy in không được để trống!",
            )
        update_data["model_name"] = clean_name
    if payload.classification is not None:
        update_data["phan_loai"] = payload.classification
    if payload.quantity is not None:
        update_data["so_luong"] = max(1, payload.quantity)
    if payload.status is not None:
        update_data["trang_thai"] = payload.status
    if "ghi_chu" in fields_set:
        update_data["ghi_chu"] = payload.ghi_chu.strip() if payload.ghi_chu else None

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vui lòng cung cấp ít nhất một thông tin cần cập nhật!",
        )

    try:
        existing_res = await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .select("*")
            .eq("id", printer_id)
            .limit(1)
            .execute()
        )
        if not existing_res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy máy dọn #{printer_id} để cập nhật!",
            )

        allowed_modify = await run_in_threadpool(
            can_modify_warranty_record, current_user, existing_res.data[0]
        )
        if not allowed_modify:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bạn chỉ được sửa bản ghi của chính mình hoặc cùng phòng ban.",
            )

        update_data["updated_by"] = username
        update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

        res = await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .update(update_data)
            .eq("id", printer_id)
            .execute()
        )

        staff_dept_map = await run_in_threadpool(get_staff_department_map)
        return _format_printer_row(res.data[0], staff_dept_map)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi cập nhật máy dọn #{printer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi cập nhật thông tin máy dọn.",
        )


# --------------------------------------------------------------------------
# 6. QUICK STATUS UPDATE (e.g., 'Sẵn có' -> 'Đã bán' hoặc 'Hoàn trả')
# --------------------------------------------------------------------------
@router.patch("/{printer_id}/status", response_model=CleanedPrinterResponse)
async def update_cleaned_printer_status(
    printer_id: int,
    payload: CleanedPrinterStatusUpdate,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    username = current_user.get("ho_ten") or current_user.get("username") or "System"
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))

    update_data = {
        "trang_thai": payload.status,
        "updated_by": username,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if "ghi_chu" in fields_set:
        update_data["ghi_chu"] = payload.ghi_chu.strip() if payload.ghi_chu else None

    try:
        res = await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .update(update_data)
            .eq("id", printer_id)
            .execute()
        )
        if not res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy bản ghi #{printer_id}!",
            )

        staff_dept_map = await run_in_threadpool(get_staff_department_map)
        return _format_printer_row(res.data[0], staff_dept_map)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi cập nhật trạng thái máy dọn #{printer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi chuyển trạng thái.",
        )


# --------------------------------------------------------------------------
# 7. DELETE CLEANED PRINTER RECORD
# --------------------------------------------------------------------------
@router.delete("/{printer_id}", status_code=status.HTTP_200_OK)
async def delete_cleaned_printer(
    printer_id: int,
    current_user: dict = Depends(require_cleaned_printers_access),
):
    try:
        existing_res = await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .select("*")
            .eq("id", printer_id)
            .limit(1)
            .execute()
        )
        if not existing_res.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Không tìm thấy bản ghi #{printer_id} để xóa!",
            )

        allowed_modify = await run_in_threadpool(
            can_modify_warranty_record, current_user, existing_res.data[0]
        )
        if not allowed_modify:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bạn chỉ được xóa bản ghi của chính mình hoặc cùng phòng ban.",
            )

        await run_in_threadpool(
            lambda: supabase_admin.table("may_don_ton_kho")
            .delete()
            .eq("id", printer_id)
            .execute()
        )

        return {"success": True, "message": f"Đã xóa thành công bản ghi máy dọn #{printer_id}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Lỗi xóa máy dọn #{printer_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lỗi hệ thống khi xóa bản ghi.",
        )