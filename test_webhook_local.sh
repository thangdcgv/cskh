#!/usr/bin/env bash

SECRET="94564b4aa582b6837e2b83f21af93b2cc5c0d8c65329f5e6d7f577b68ad3c242"
BASE_URL="http://127.0.0.1:8000/api/webhook/base"

WF_REPAIR="18190"
WF_STORAGE="18720"
WF_DELIVERY="18249"

EXISTING_PHONE="0931334405"
EXISTING_CODE="DT1119-86600"
EXISTING_BASE_JOB_ID="4333421"

NEW_JOB_STORAGE="999001"
NEW_JOB_DELIVERY="999002"
NEW_JOB_REPAIR="999003"

echo "=================================================="
echo "TEST A — Forward sang LƯU KHO (khớp theo SĐT, cột Máy Sửa Ở Cty)"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=storage" \
  --data-urlencode "job[id]=$NEW_JOB_STORAGE" \
  --data-urlencode "job[name]=[Fwd] Canon 6770 - $EXISTING_PHONE" \
  --data-urlencode "workflow[id]=$WF_STORAGE" \
  --data-urlencode "stage[name]=Máy Sửa Ở Cty" \
  --data-urlencode "custom_fields[dien_thoai]=$EXISTING_PHONE" \
  --data-urlencode "custom_fields[dia_chi]=123 Nguyen Trai, Q1" \
  --data-urlencode "custom_fields[ghi_chu]=Test forward luu kho thanh cong"
echo; echo

echo "=================================================="
echo "TEST B — Forward sang LƯU KHO, kỳ vọng BỊ CHẶN (SĐT lạ)"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=storage" \
  --data-urlencode "job[id]=$NEW_JOB_STORAGE" \
  --data-urlencode "job[name]=[Fwd] Sửa chữa -> Lưu kho" \
  --data-urlencode "workflow[id]=$WF_STORAGE" \
  --data-urlencode "stage[name]=Máy Sửa Ở Cty" \
  --data-urlencode "custom_fields[dien_thoai]=0999999999"
echo; echo

echo "=================================================="
echo "TEST C — Forward sang LƯU KHO khớp theo PARENT_ID (thiếu workflow[id])"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=storage" \
  --data-urlencode "job[id]=$NEW_JOB_STORAGE" \
  --data-urlencode "job[name]=[Fwd] Chuyen luu kho theo parent_id" \
  --data-urlencode "stage[name]=Máy Sửa Ở Cty" \
  --data-urlencode "parent_id=$EXISTING_BASE_JOB_ID"
echo; echo

echo "=================================================="
echo "TEST D — Forward sang GIAO NHẬN (cột Thông Tin Đơn -> Chành xe)"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=delivery" \
  --data-urlencode "job[id]=$NEW_JOB_DELIVERY" \
  --data-urlencode "job[name]=[Fwd] $EXISTING_CODE Xe To Chau - $EXISTING_PHONE" \
  --data-urlencode "stage[name]=Thông Tin Đơn" \
  --data-urlencode "custom_fields[thong_tin_chanh_xe_chuyen_phat]=Chành xe Tô Châu - Cần Thơ"
echo; echo

echo "=================================================="
echo "TEST E — Cập nhật GIAO NHẬN sang cột Xử lý đơn hàng"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=delivery" \
  --data-urlencode "job[id]=$NEW_JOB_DELIVERY" \
  --data-urlencode "job[name]=[Fwd] $EXISTING_CODE Xe To Chau - $EXISTING_PHONE" \
  --data-urlencode "stage[name]=Xử lý đơn hàng"
echo; echo

echo "=================================================="
echo "TEST F — Tạo phiếu SỬA CHỮA mới (workflow repair)"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}&wf=repair" \
  --data-urlencode "job[id]=$NEW_JOB_REPAIR" \
  --data-urlencode "job[name]=Sua may in Epson - 0911111111 - Tran Thi B" \
  --data-urlencode "workflow[id]=$WF_REPAIR" \
  --data-urlencode "stage[name]=Tiếp nhận" \
  --data-urlencode "custom_fields[dien_thoai]=0911111111" \
  --data-urlencode "custom_fields[dia_chi]=456 Le Loi, Q3"
echo; echo

echo "=================================================="
echo "TEST G — Thiếu ?wf và workflow[id] RỖNG + SĐT lạ, kỳ vọng BỊ CHẶN"
echo "=================================================="
curl -g -s -X POST "${BASE_URL}?secret=${SECRET}" \
  --data-urlencode "job[id]=999004" \
  --data-urlencode "job[name]=[Fwd] May la - 0922222222" \
  --data-urlencode "stage[name]=Máy Sửa Ở Cty" \
  --data-urlencode "custom_fields[dien_thoai]=0922222222"
echo; echo

echo "XONG."
