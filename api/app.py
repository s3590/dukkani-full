# مسار الملف: api/app.py
import os
import asyncio
from aiohttp import web
import database
from bot.setup import bot, dp

# استيراد المسارات والـ Middlewares
from api.middlewares import error_middleware, maintenance_middleware, auth_and_shadow_middleware, rate_limit_middleware
from api.views import setup_views
from api.routes.customer import setup_customer_routes
from api.routes.pos import setup_pos_routes
from api.routes.manage import setup_manage_routes
from api.routes.misc import setup_misc_routes
from api.routes.websockets import setup_websocket_routes
from api.routes.admin import setup_admin_routes

async def background_init(app ):
    """مهمة خلفية لتشغيل الإعدادات الثقيلة دون تأخير إقلاع السيرفر"""
    try:
        await database.init_db()
        async with database.pool.acquire() as conn:
            # 🛡️ الحماية من فقدان اشتراكات الإشعارات بعد إعادة تشغيل السيرفر
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    user_id INT,
                    user_type VARCHAR(20),
                    subscription_json TEXT,
                    PRIMARY KEY (user_id, user_type)
                );
            """)
        await bot.delete_webhook(drop_pending_updates=True)
        asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        print("✅ تم تشغيل النظام بنجاح (الويب + البوت)!")
    except Exception as e:
        print(f"❌ خطأ أثناء التهيئة الخلفية: {e}")

async def on_startup(app):
    # تهيئة قائمة لحفظ اتصالات الـ WebSockets النشطة لإغلاقها بأمان لاحقاً
    app['websockets'] = set()
    
    # 🚀 السحر هنا: إرسال مهام قاعدة البيانات والبوت للخلفية لكي يفتح المنفذ فوراً!
    asyncio.create_task(background_init(app))

async def on_cleanup(app):
    """دالة الإغلاق الآمن (Graceful Shutdown)"""
    print("⚠️ جاري إغلاق السيرفر بأمان...")

    # 1. إغلاق جميع اتصالات الـ WebSockets المفتوحة بهدوء
    for ws in set(app['websockets']):
        if not ws.closed:
            await ws.close(code=1001, message=b'Server is restarting')
    app['websockets'].clear()
    
    # 2. إغلاق اتصال البوت (Telegram Session)
    if bot.session:
        await bot.session.close()

    # 3. إغلاق بركة اتصالات قاعدة البيانات
    if database.pool:
        await database.pool.close()
        
    # 4. إغلاق مستمع رديس (Redis Listener) بأمان
    if 'redis_listener_task' in app:
        app['redis_listener_task'].cancel()
        try:
            await app['redis_listener_task']
        except asyncio.CancelledError:
            pass
            
    print("🛑 تم إغلاق جميع الاتصالات بنجاح.")

def create_app():
    # ترتيب الـ Middlewares مهم جداً هنا
    app = web.Application(middlewares=[
        error_middleware,           # 1. صائد الأخطاء الشامل
        maintenance_middleware,     # 2. جدار الصيانة
        rate_limit_middleware,      # 3. 🛡️ جدار حماية التخمين (الجديد)
        auth_and_shadow_middleware  # 4. المصادقة وفك التشفير
    ])
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup) # 👈 تسجيل دالة الإغلاق الآمن

    os.makedirs("static", exist_ok=True)
    app.router.add_static('/static/', path='static', name='static')
    
    setup_views(app)
    setup_customer_routes(app)
    setup_pos_routes(app)
    setup_manage_routes(app)
    setup_misc_routes(app)
    setup_websocket_routes(app)
    setup_admin_routes(app)
    
    return app
