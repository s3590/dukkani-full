// مسار الملف: firebase-messaging-sw.js

// ================= 1. إعدادات Firebase للإشعارات =================
importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-app.js' );
importScripts('https://www.gstatic.com/firebasejs/8.10.1/firebase-messaging.js' );

firebase.initializeApp({
  apiKey: "AIzaSyDvCRGzkVUgZodqd63QZCkwjfCjOhc6nYo",
  authDomain: "dukkani-app-30b43.firebaseapp.com",
  projectId: "dukkani-app-30b43",
  storageBucket: "dukkani-app-30b43.firebasestorage.app",
  messagingSenderId: "757873553883",
  appId: "1:757873553883:web:44c531dfc7dbe1f807f7fe"
});

const messaging = firebase.messaging();

// 🚨 تم حذف دالة onBackgroundMessage عمداً لترك فايربيس يعرض الإشعارات بأقصى سرعة وبدون أي تعارض أو إخفاء.

// ================= 2. التفاعل الصاروخي مع الإشعار (Deep Linking) =================
self.addEventListener('notificationclick', function(event) {
    event.notification.close(); // إغلاق الإشعار فوراً عند النقر
    event.stopImmediatePropagation(); // 🚀 السطر الذهبي: منع المتصفح من التأخير

    // 💡 استخراج المسار الذكي
    let route = '';
    if (event.notification.data) {
        if (event.notification.data.FCM_MSG && event.notification.data.FCM_MSG.data) {
            route = event.notification.data.FCM_MSG.data.route || event.notification.data.FCM_MSG.data.action || '';
        } else {
            route = event.notification.data.route || event.notification.data.action || '';
        }
    }
    
    const targetUrl = new URL('/?action=' + route, self.location.origin).href;
    
    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(clientList) {
            // 1. هل التطبيق مفتوح في الخلفية؟
            for (let i = 0; i < clientList.length; i++) {
                let client = clientList[i];
                if (client.url.includes(self.location.origin) && 'focus' in client) {
                    // ⚡ إرسال رسالة صامتة للتطبيق المفتوح لتغيير الشاشة فوراً
                    client.postMessage({
                        type: 'DEEP_LINK',
                        route: route
                    });
                    return client.focus(); // جلب التطبيق للأمام بلمح البصر
                }
            }
            // 2. إذا كان مغلقاً تماماً، نفتحه ونمرر المسار في الرابط
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});

// ================= 3. إعدادات الكاش (PWA) =================
const CACHE_NAME = 'dukkani-saas-v11'; // 🚀 تم رفع الإصدار لإجبار المتصفحات على تحديث الكود فوراً

const CORE_ASSETS = [
    '/',
    '/static/logo_customer.png',
    'https://fonts.googleapis.com/css2?family=Cairo:wght@400;700;900&display=swap'
];

self.addEventListener('install', (event ) => {
    self.skipWaiting(); // تفعيل مباشر دون انتظار
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return cache.addAll(CORE_ASSETS);
        })
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cache) => {
                    if (cache !== CACHE_NAME) {
                        console.log('🧹 [Service Worker] تم مسح الكاش القديم وتفعيل الإصدار الجديد');
                        return caches.delete(cache);
                    }
                })
            );
        })
    );
    self.clients.claim(); // السيطرة الفورية على جميع النوافذ المفتوحة
});

self.addEventListener('fetch', (event) => {
    const url = event.request.url;

    // استثناء طلبات الـ API و WebSockets من الكاش لضمان جلب البيانات الحية
    if (url.includes('/api/') || url.includes('/ws/')) {
        return; 
    }

    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).then((networkResponse) => {
                return caches.open(CACHE_NAME).then((cache) => {
                    cache.put(event.request, networkResponse.clone());
                    return networkResponse;
                });
            }).catch(() => {
                return caches.match(event.request, {ignoreSearch: true});
            })
        );
        return;
    }

    event.respondWith(
        caches.match(event.request, {ignoreSearch: true}).then((cachedResponse) => {
            const fetchPromise = fetch(event.request).then((networkResponse) => {
                if (networkResponse && (networkResponse.status === 200 || networkResponse.type === 'opaque')) {
                    let responseToCache = networkResponse.clone();
                    caches.open(CACHE_NAME).then((cache) => {
                        cache.put(event.request, responseToCache);
                    });
                }
                return networkResponse;
            }).catch(() => {});
            return cachedResponse || fetchPromise;
        })
    );
});
