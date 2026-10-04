import os
import time
import aiohttp  # 👈 أضف هذا السطر
from aiohttp import web
import database
from bot.setup import bot
from config import ADMIN_ID, BOT_TOKEN
from api.middlewares import get_user_from_token

# 👇 ذاكرة مؤقتة لروابط الصور وملفاتها (تمنع حظر تيليجرام وتسرع التحميل 100 ضعف ) 👇
IMAGE_CACHE = {}

async def serve_telegram_image(request: web.Request):
    file_id = request.match_info.get('file_id')
    if not file_id or file_id == 'null':
        return web.HTTPFound('/static/logo_customer.png')
        
    current_time = time.time()
    
    # 🛡️ تنظيف الذاكرة المؤقتة (Garbage Collection) لمنع تسريب الرام
    # إذا تجاوز القاموس 500 صورة، نقوم بمسح الصور المنتهية الصلاحية
    if len(IMAGE_CACHE) > 500:
        expired_keys = [k for k, v in IMAGE_CACHE.items() if current_time - v[2] > 3000]
        for k in expired_keys:
            del IMAGE_CACHE[k]
            
        # حماية إضافية: إذا كان الضغط هائلاً ولا تزال الذاكرة ممتلئة، نمسح أقدم 100 صورة عشوائياً
        if len(IMAGE_CACHE) > 1000:
            for k in list(IMAGE_CACHE.keys())[:100]:
                del IMAGE_CACHE[k]
    
    # 1. التحقق من الذاكرة المؤقتة (الصورة صالحة لمدة 50 دقيقة = 3000 ثانية)
    if file_id in IMAGE_CACHE:
        cached_data, content_type, timestamp = IMAGE_CACHE[file_id]
        if current_time - timestamp < 3000:
            return web.Response(body=cached_data, content_type=content_type)
            
    # 2. إذا لم تكن في الذاكرة، نطلبها من تيليجرام كملف حقيقي (لكي يقبل متصفح كروم تثبيت التطبيق)
    try:
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
        
        async with aiohttp.ClientSession( ) as session:
            async with session.get(file_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    content_type = resp.headers.get('Content-Type', 'image/jpeg')
                    
                    # حفظ البيانات الفعلية في الذاكرة المؤقتة للمرات القادمة
                    IMAGE_CACHE[file_id] = (data, content_type, current_time)
                    
                    return web.Response(body=data, content_type=content_type)
    except Exception as e:
        import logging
        logging.error(f"Image fetch error: {e}")
        
    return web.HTTPFound('/static/logo_customer.png')

async def api_log_error(request: web.Request):
    """صائد الأخطاء القادمة من شاشات المستخدمين (Frontend)"""
    try:
        data = await request.json()
        err_msg = data.get('error', 'Unknown error')
        
        # 🕵️‍♂️ محاولة معرفة من هو المستخدم
        user_info = "مجهول / زائر"
        try:
            auth_header = request.headers.get('Authorization')
            if auth_header and auth_header.startswith('Bearer '):
                import jwt
                from config import JWT_SECRET
                token = auth_header.split(' ')[1]
                user = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
                
                if 'customer_id' in user:
                    user_info = f"🛒 زبون رقم: {user['customer_id']} | بقالة رقم: {user.get('store_id')}"
                elif 'worker_id' in user:
                    user_info = f"👷‍♂️ عامل رقم: {user['worker_id']} | بقالة رقم: {user.get('store_id')}"
                elif 'store_id' in user:
                    user_info = f"🏪 صاحب بقالة رقم: {user['store_id']}"
        except:
            pass
        
        import asyncio
        from bot.setup import bot
        from config import ADMIN_ID
        import database
        
        # 1. إرسال تنبيه فوري للمدير في تيليجرام
        msg = (
            f"📱 **خطأ في واجهة التطبيق (Frontend):**\n"
            f"👤 **المستخدم:** {user_info}\n\n"
            f"`{err_msg[:3500]}`"
        )
        asyncio.create_task(bot.send_message(ADMIN_ID, msg, parse_mode="Markdown"))
        
        # 2. توثيق الخطأ في الرادار (لوحة تحكم الإدارة)
        async with database.pool.acquire() as conn:
            await conn.execute("INSERT INTO admin_logs (action, details) VALUES ($1, $2)", "خطأ واجهة 📱", f"المستخدم: {user_info}\n{err_msg[:150]}")
            
    except Exception: 
        pass
    return web.json_response({"status": "logged"})

async def serve_firebase_sw(request: web.Request):
    # تأكد من وجود ملف firebase-messaging-sw.js في المجلد الرئيسي أو مجلد static
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sw_path = os.path.join(base_dir, 'firebase-messaging-sw.js')
    if os.path.exists(sw_path):
        with open(sw_path, 'r', encoding='utf-8') as f:
            return web.Response(text=f.read(), content_type='application/javascript')
    return web.Response(text="", status=404)

async def api_save_subscription(request: web.Request):
    """حفظ اشتراكات الإشعارات مع ميزة التنظيف الذكي (Token Stealing Protection)"""
    user = get_user_from_token(request)
    data = await request.json()
    token = data.get("subscription")
    
    # تحويل التوكن إلى نص إذا كان قادماً من متصفح (Dict)
    if isinstance(token, dict):
        import json
        token = json.dumps(token)
    
    if token:
        user_id = None
        user_type = 'customer'
        
        if 'customer_id' in user:
            user_id = user['customer_id']
            user_type = 'customer'
        elif 'agent_id' in user:
            user_id = user['agent_id']
            user_type = 'agent'
        elif 'store_id' in user:
            user_id = user.get('worker_id') or user['store_id']
            user_type = 'worker' if 'worker_id' in user else 'store'
            
        if user_id:
            async with database.pool.acquire() as conn:
                # 🌟 السحر المسروق: مسح التوكن من أي حساب آخر استخدم هذا الهاتف سابقاً!
                await conn.execute("DELETE FROM push_subscriptions WHERE subscription_json = $1 AND (user_id != $2 OR user_type != $3)", token, user_id, user_type)
                
                # حفظ التوكن للحساب الحالي بشكل نظيف
                await conn.execute("""
                    INSERT INTO push_subscriptions (user_id, user_type, subscription_json)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, user_type) DO UPDATE 
                    SET subscription_json = EXCLUDED.subscription_json
                """, user_id, user_type, token)
            
    return web.json_response({"status": "success"})

async def serve_assetlinks(request: web.Request):
    assetlinks_data = [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "com.dukkani.app",
            "sha256_cert_fingerprints": ["9F:39:7F:09:D9:1F:13:EA:1E:53:65:EB:D5:C7:FF:BF:FF:D4:A4:0C:8A:F8:88:30:E3:58:CD:65:43:38:18:7D"]
        }
    }]
    return web.json_response(assetlinks_data)

async def serve_dynamic_manifest(request: web.Request):
    """صناعة ملف PWA ديناميكي لكل بقالة باسمها وشعارها"""
    store_id = request.query.get('store_id')
    
    # القيم الافتراضية إذا لم يتم تمرير رقم البقالة
    app_name = "دُكّاني"
    theme_color = "#0ba360"
    logo_url = "/static/logo_customer.png"
    start_url = "/"
    
    if store_id and store_id.isdigit():
        try:
            async with database.pool.acquire() as conn:
                store = await conn.fetchrow("SELECT name, theme_color, logo_url FROM stores WHERE id = $1", int(store_id))
                if store:
                    app_name = store['name']
                    theme_color = store['theme_color'] or "#0ba360"
                    logo_url = store['logo_url'] or "/static/logo_customer.png"
                    start_url = f"/?store={store_id}"
        except Exception:
            pass

    manifest = {
        "id": f"dukkani-store-{store_id or 'default'}",
        "name": app_name,
        "short_name": app_name,
        "description": f"اطلب مقاضيك بسهولة من {app_name}",
        "start_url": start_url,
        "display": "standalone",
        "background_color": "#f8fafc",
        "theme_color": theme_color,
        "orientation": "portrait",
        "dir": "rtl",
        "lang": "ar",
        "categories": ["shopping", "finance", "lifestyle"],
        "icons": [
            {
                "src": logo_url,
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": logo_url,
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any"
            },
            {
                "src": logo_url,
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable"
            }
        ]
    }
    
    return web.json_response(manifest, content_type='application/manifest+json')

def setup_misc_routes(app: web.Application):
    """تسجيل المسارات المتنوعة في التطبيق"""
    app.router.add_get('/api/image/{file_id}', serve_telegram_image)
    app.router.add_post('/api/log_error', api_log_error)
    app.router.add_get('/firebase-messaging-sw.js', serve_firebase_sw)
    app.router.add_post('/api/subscribe', api_save_subscription)
    app.router.add_get('/.well-known/assetlinks.json', serve_assetlinks)
    app.router.add_get('/api/manifest', serve_dynamic_manifest)
