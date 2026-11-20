#!/usr/bin/env bash
set -e

URL="http://127.0.0.1:8000/api/webhook/base"
SECRET="94564b4aa582b6837e2b83f21af93b2cc5c0d8c65329f5e6d7f577b68ad3c242"

# Đổi lại nếu system_settings của bạn đang cấu hình ID workflow test
WF_REPAIR="20025"
WF_STORAGE="20036"

echo "=== BƯỚC 1: Tạo & Hoàn thành job bên Sửa chữa (Job ID: 4249464) ==="
curl -s -X POST "$URL" \
  -H "x-base-secret: $SECRET" \
  --data-urlencode "id=4249464" \
  --data-urlencode "name=Tuấn 0909275609 Test" \
  --data-urlencode "workflow_id=$WF_REPAIR" \
  --data-urlencode "state=done" \
  --data-urlencode "stage={\"id\":\"139702\",\"name\":\"Done\",\"metatype\":\"done\",\"workflow_id\":\"$WF_REPAIR\",\"type\":\"workflowstages\"}" \
  --data-urlencode "custom_dia_chi=G2010 + usb" \
  --data-urlencode "custom_so_serial=ahhh1111" \
  --data-urlencode "custom_hinh_anh_giao_nhan_hang_hoa_chan-fids=WyIyMDI2MTA6c3lzMTExOTU6d29ya2Zsb3c6MTk6MTc6RkNGR0wyTVI0UTRPUkhBSk84MVc6ZjQ3MjIzMjQyN2Y4ODViYmQ4ZDFlZTE1NWViODlhOTI1OGM0NTQ3YzpJS1BaWUxFS0dWS1ExRVVEQzJERlc0RVNCVFNUM1lJSy5ZRkJJQUk1WlIzVEVSSzFGSk83WklITkNQRkRaSlk5Ty1DWFdYWjFSRkcyODE5QzdWVkcxSUJSS0RXOERYSDVaVS1jYW5jZWxfYWxsLnBuZyJd" \
  | python3 -m json.tool

sleep 1

echo -e "\n=== BƯỚC 2: Forward sang Lưu kho (Job ID mới: 4249999, không có ID gốc) ==="
curl -s -X POST "$URL" \
  -H "x-base-secret: $SECRET" \
  --data-urlencode "id=4249999" \
  --data-urlencode "name=Tuấn 0909275609 Test" \
  --data-urlencode "workflow_id=$WF_STORAGE" \
  --data-urlencode "state=active" \
  --data-urlencode "forwarded_from=0" \
  --data-urlencode "gen_prev=null" \
  --data-urlencode "gen_list=null" \
  --data-urlencode "stage={\"id\":\"140101\",\"name\":\"Đang lưu kho\",\"metatype\":\"normal\",\"workflow_id\":\"$WF_STORAGE\",\"type\":\"workflowstages\"}" \
  --data-urlencode "custom_nhieu_hinh_anh_hoa_don_hang_da_s-fids=WyIyMDI2MTE6c3lzMTExOTU6d29ya2Zsb3c6MjA6MTk6UklCUEw0R1VHSkZXT1E5TTlRWk06ZDc1NzIzOTA3MDU2NmExYjcyMjgzODFhNTg3NWY2ZjZlNmFhNDJkNjpTSko5SlNXWktaWjhNT0E4STFVQ1lINFpBQ1ZIQzdINi5GQkgzRlVBS1RWT1FFOFFaT0FSNkE5VU5UV1c5NzJVNi1JVlQ0QUxMUTdXUTVGNUFKQUlKV1pURzkxM0xFQ0g1Ti1zY3JlZW5zaG90XzIwMjZfMTFfMDhfYXRfMTIuMjAuMDYucG5nIl0" \
  | python3 -m json.tool

sleep 1

echo -e "\n=== BƯỚC 3: Hoàn tất Lưu kho (Stage Done -> dispatched / returned) ==="
curl -s -X POST "$URL" \
  -H "x-base-secret: $SECRET" \
  --data-urlencode "id=4249999" \
  --data-urlencode "name=Tuấn 0909275609 Test" \
  --data-urlencode "workflow_id=$WF_STORAGE" \
  --data-urlencode "state=done" \
  --data-urlencode "stage={\"id\":\"140102\",\"name\":\"Done\",\"metatype\":\"done\",\"workflow_id\":\"$WF_STORAGE\",\"type\":\"workflowstages\"}" \
  | python3 -m json.tool