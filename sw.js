const CACHE_NAME = 'center-hub-v1';

// Sự kiện cài đặt Service Worker
self.addEventListener('install', (event) => {
    self.skipWaiting();
});

// Sự kiện kích hoạt
self.addEventListener('activate', (event) => {
    event.waitUntil(clients.claim());
});

// Xử lý fetch cơ bản (để ứng dụng hoạt động mượt mà)
self.addEventListener('fetch', (event) => {
    // Với các request động từ server FastAPI, ưu tiên lấy mạng trước
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request);
        })
    );
});