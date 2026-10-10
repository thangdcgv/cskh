import asyncio
import logging
from typing import Any
from fastapi import WebSocket
from datetime import timezone, timedelta

# Định nghĩa múi giờ Việt Nam (UTC+7)
VN_TZ = timezone(timedelta(hours=7))

logger = logging.getLogger("uvicorn.error")


class ConnectionManager:
    def __init__(self):
        # Dùng set để lưu danh sách kết nối duy nhất
        self._active_connections: set[WebSocket] = set()
        # Khóa bảo vệ truy cập đồng bộ giữa các coroutines
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Kết nối mới — tự động chấp nhận & đăng ký"""
        await websocket.accept()
        async with self._lock:
            self._active_connections.add(websocket)
        logger.info(f"WebSocket kết nối mới — Tổng: {len(self._active_connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Rút kết nối ra khỏi danh sách — an toàn kể cả khi đã bị loại bỏ trước đó"""
        async with self._lock:
            self._active_connections.discard(websocket)
        logger.info(f"WebSocket ngắt kết nối — Còn lại: {len(self._active_connections)}")

    async def broadcast(self, message: dict[str, Any]) -> int:
        """Gửi song song (concurrent) tới tất cả client + tự động dọn kết nối hỏng"""
        async with self._lock:
            active = list(self._active_connections)  # Snapshot danh sách kết nối

        if not active:
            return 0

        # Tạo danh sách các task gửi tin nhắn đồng thời
        tasks = [conn.send_json(message) for conn in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        dead: set[WebSocket] = set()
        for conn, result in zip(active, results):
            if isinstance(result, Exception):
                dead.add(conn)
                logger.debug(f"WebSocket gửi thất bại đến client: {result}")

        # Dọn dẹp các kết nối hỏng
        if dead:
            async with self._lock:
                for conn in dead:
                    self._active_connections.discard(conn)
            logger.info(f"Đã dọn dẹp {len(dead)} kết nối WebSocket hỏng")

        return len(self._active_connections)

    @property
    def count(self) -> int:
        """Số lượng kết nối WebSocket đang hoạt động"""
        return len(self._active_connections)


# Khởi tạo duy nhất toàn cục (Singleton)
manager = ConnectionManager()