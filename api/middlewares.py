# مسار الملف: api/middlewares.py
from aiohttp import web
import jwt
import time
import traceback
import logging
import urllib.parse
import hashlib
import hmac
import json
from config import JWT_SECRET, ADMIN_ID, BOT_TOKEN
from bot.setup import bot
import database

# ذاكرة مؤقتة لحالة الصيانة (تتحدث كل 15 ثانية لتخفيف الضغط عن قاعدة البيانات )
MAINTENANCE_CACHE = {"status": False, "last_checked": 0}
CACHE_TTL = 15 

# ذاكرة مؤقتة لحماية التخمين (Rate Limiting)
RATE_LIMIT_CACHE = {}
MAX_ATTEMPTS = 5      # أقصى عدد للمحاولات الفاشلة
BLOCK_TIME = 900      # مدة الحظر بالثواني (15 دقيقة)

def get_user_from_token(request: web.Request):
    """دالة مساعدة لاستخراج بيانات الزبون (تقرأ من الذاكرة لتجنب فك التشفير المزدوج)"""
    if 'user' in request:
        return request['user']
        
    # كخيار احتياطي إذا لم يمر الطلب على الـ Middleware
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        raise web.HTTPUnauthorized()
    token = auth_header.split(' ')[1]
    return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])

def validate_telegram_data(init_data: str) -> dict:
    """دالة للتحقق من صحة بيانات تيليجرام (initData) القادمة من الواجهة الأمامية"""
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        if 'hash' not in parsed_data: return None
        
        # 🛡️ حماية Replay Attack: التحقق من أن البيانات ليست قديمة (أكثر من 24 ساعة)
        auth_date = int(parsed_data.get('auth_date', 0))
        if time.time() - auth_date > 86400:
            logging.warning("⚠️ محاولة استخدام بيانات تيليجرام منتهية الصلاحية!")
            return None
            
        received_hash = parsed_data.pop('hash')
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == received_hash:
            return json.loads(parsed_data.get('user', '{}'))
        return None
    except Exception as e:
        logging.error(f"Telegram Auth Error: {e}")
        return None

@web.middleware
async def maintenance_middleware(request: web.Request, handler):
    """التحقق من وضع الصيانة (مع نظام Cache ذكي)"""
    # 1. تجاهل الملفات الثابتة ومسارات الإدارة العليا
    if not request.path.startswith('/api/') or request.path.startswith('/api/admin'):
        return await handler(request)
        
    # 2. التحقق من الذاكرة المؤقتة (Cache) بدلاً من قاعدة البيانات في كل طلب
    current_time = time.time()
    if current_time - MAINTENANCE_CACHE["last_checked"] > CACHE_TTL:
        try:
            async with database.pool.acquire() as conn:
                MAINTENANCE_CACHE["status"] = await conn.fetchval("SELECT maintenance_mode FROM system_settings WHERE id = 1")
            MAINTENANCE_CACHE["last_checked"] = current_time
        except Exception:
            pass # في حال لم يتم إنشاء الجدول بعد
            
    if MAINTENANCE_CACHE["status"]:
        return web.json_response({
            "status": "error", 
            "message": "النظام تحت الصيانة والتحديث حالياً 🛠️. يرجى المحاولة بعد قليل."
        }, status=503)
        
    return await handler(request)

@web.middleware
async def auth_and_shadow_middleware(request: web.Request, handler):
    """نظام المصادقة المزدوج (JWT + Telegram Web App) مع حماية وضع الظل"""
    if not request.path.startswith('/api/'):
        return await handler(request)

    auth_header = request.headers.get('Authorization', '')
    is_manage_route = request.path.startswith('/api/manage/')
    auth_success = False

    # 1. دعم وضع الظل (المدير العام) والمناديب والزبائن عبر JWT
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            request['user'] = payload
            request['safe_telegram_id'] = payload.get('admin_id') or payload.get('agent_id') or payload.get('store_id')
            request['auth_role'] = payload.get('role', 'admin')
            
            # التحقق من القائمة السوداء (فقط إذا كان توكن دعم فني)
            if payload.get('role') == 'store_owner' and 'jti' in payload:
                async with database.pool.acquire() as conn:
                    is_blacklisted = await conn.fetchval("SELECT 1 FROM blacklisted_tokens WHERE jti = $1", payload['jti'])
                    if is_blacklisted:
                        return web.json_response({"status": "error", "message": "تم إنهاء جلسة الدعم الفني (الظل) 🛑"}, status=401)
            
            auth_success = True
        except Exception:
            pass # نترك الدوال الأخرى تتعامل مع التوكنات المنتهية

    # 2. دعم أصحاب البقالات والعمال عبر Telegram Web App
    elif auth_header.startswith('tma '):
        init_data = auth_header[4:]
        user_data = validate_telegram_data(init_data)
        if user_data and 'id' in user_data:
            request['safe_telegram_id'] = user_data['id']
            request['auth_role'] = 'store_user'
            auth_success = True

    # 🛡️ فرض الحماية الصارمة فقط على مسارات الإدارة
    if is_manage_route and not auth_success:
        return web.json_response({"status": "error", "message": "غير مصرح: بيانات المصادقة مفقودة أو غير صالحة"}, status=401)
        
    return await handler(request)

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """نظام حماية التخمين (Rate Limiting) لمسارات تسجيل الدخول"""
    # نطبق الحماية فقط على مسارات تسجيل الدخول الحساسة
    if request.path in ['/api/admin/login', '/api/customer/login', '/api/worker/login']:
        # جلب عنوان الـ IP الحقيقي (حتى لو كان خلف Cloudflare أو Nginx)
        ip = request.headers.get('X-Forwarded-For', request.remote)
        if ip: ip = ip.split(',')[0].strip()
        
        now = time.time()
        
        # 1. التحقق مما إذا كان الـ IP محظوراً
        if ip in RATE_LIMIT_CACHE:
            attempts, block_until = RATE_LIMIT_CACHE[ip]
            if now < block_until:
                remaining_mins = int((block_until - now) / 60) or 1
                return web.json_response({
                    "status": "error", 
                    "message": f"تم حظر عنوان IP الخاص بك مؤقتاً بسبب كثرة المحاولات الفاشلة. يرجى المحاولة بعد {remaining_mins} دقيقة 🛑."
                }, status=429)
            elif now > block_until and block_until > 0:
                # انتهت فترة الحظر، نصفر العداد
                RATE_LIMIT_CACHE[ip] = [0, 0]
                
        # 2. تنفيذ الطلب
        response = await handler(request)
        
        # 3. تحليل النتيجة (إذا كان الرد خطأ أو 401، نزيد العداد)
        is_failed_attempt = False
        if response.status in [401, 403]:
            is_failed_attempt = True
        elif response.status == 200 and response.content_type == 'application/json':
            try:
                body = json.loads(response.text)
                if body.get('status') == 'error':
                    is_failed_attempt = True
            except: pass

        if is_failed_attempt:
            if ip not in RATE_LIMIT_CACHE:
                RATE_LIMIT_CACHE[ip] = [0, 0]
            
            RATE_LIMIT_CACHE[ip][0] += 1
            
            # إذا تجاوز الحد المسموح، نقوم بحظره
            if RATE_LIMIT_CACHE[ip][0] >= MAX_ATTEMPTS:
                RATE_LIMIT_CACHE[ip][1] = now + BLOCK_TIME
                logging.warning(f"🚨 [أمان]: تم حظر الـ IP ({ip}) لمدة 15 دقيقة بسبب محاولات الدخول المتكررة.")
                
        elif response.status == 200:
            # نجاح تسجيل الدخول، نصفر العداد لهذا الـ IP
            if ip in RATE_LIMIT_CACHE:
                RATE_LIMIT_CACHE[ip] = [0, 0]
                
        return response
        
    return await handler(request)

@web.middleware
async def error_middleware(request: web.Request, handler):
    """صائد الأخطاء الشامل لسيرفر الويب (مع تحديد هوية المستخدم)"""
    try:
        return await handler(request)
    except web.HTTPException as ex:
        raise ex
    except Exception as e:
        import traceback
        import asyncio
        from bot.setup import bot
        from config import ADMIN_ID
        
        err_str = traceback.format_exc()
        
        # 🕵️‍♂️ محاولة معرفة من هو المستخدم الذي واجه الخطأ
        user_info = "مجهول / غير مسجل دخول"
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
                elif 'agent_id' in user:
                    user_info = f"🛵 مندوب رقم: {user['agent_id']} | بقالة رقم: {user.get('store_id')}"
                elif 'store_id' in user:
                    user_info = f"🏪 صاحب بقالة رقم: {user['store_id']}"
        except:
            pass

        # 🚀 إرسال رسالة مفصلة للمدير العام
        msg = (
            f"🚨 **خطأ برمجي في السيرفر (Backend):**\n"
            f"👤 **الضحية:** {user_info}\n"
            f"🔗 **المسار:** `{request.path}`\n\n"
            f"`{err_str[:3500]}`"
        )
        
        try:
            asyncio.create_task(bot.send_message(ADMIN_ID, msg, parse_mode="Markdown"))
        except: pass
        
        return web.json_response({"status": "error", "message": "حدث خطأ داخلي وتم إبلاغ الإدارة فوراً لإصلاحه 🛠️."}, status=500)

# 🛡️ ذاكرة مؤقتة لتتبع محاولات الدخول الفاشلة (IP Rate Limiting)
LOGIN_ATTEMPTS_CACHE = {}
MAX_FAILED_ATTEMPTS = 5
BLOCK_DURATION = 900 # 15 دقيقة بالثواني

@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    """جدار حماية لمنع هجمات التخمين (Brute Force) على مسارات تسجيل الدخول"""
    if request.path.endswith('/login'):
        # الحصول على الـ IP الحقيقي (حتى لو كان خلف Cloudflare أو Proxy)
        client_ip = request.headers.get('X-Forwarded-For', request.remote)
        if client_ip:
            client_ip = client_ip.split(',')[0].strip()
            
        now = time.time()
        
        # 1. التحقق مما إذا كان الـ IP محظوراً
        if client_ip in LOGIN_ATTEMPTS_CACHE:
            attempts, block_until = LOGIN_ATTEMPTS_CACHE[client_ip]
            if block_until and now < block_until:
                remaining_mins = int((block_until - now) / 60)
                return web.json_response({
                    "status": "error", 
                    "message": f"🚨 تم حظر جهازك مؤقتاً بسبب كثرة المحاولات الخاطئة. حاول مجدداً بعد {remaining_mins} دقيقة."
                }, status=429)
            elif block_until and now >= block_until:
                # فك الحظر إذا انتهى الوقت
                LOGIN_ATTEMPTS_CACHE.pop(client_ip, None)
                
        # 2. تنفيذ الطلب
        response = await handler(request)
        
        # 3. تحليل النتيجة (إذا كان الدخول فاشلاً نزيد العداد)
        if response.status == 200 and client_ip:
            try:
                body = json.loads(response.text)
                if body.get('status') == 'error':
                    attempts, _ = LOGIN_ATTEMPTS_CACHE.get(client_ip, (0, 0))
                    attempts += 1
                    if attempts >= MAX_FAILED_ATTEMPTS:
                        LOGIN_ATTEMPTS_CACHE[client_ip] = (attempts, now + BLOCK_DURATION)
                        logging.warning(f"🚨 تم حظر الـ IP: {client_ip} لمحاولته اختراق النظام!")
                    else:
                        LOGIN_ATTEMPTS_CACHE[client_ip] = (attempts, 0)
                elif body.get('status') == 'success':
                    # تصفير العداد عند الدخول الناجح
                    LOGIN_ATTEMPTS_CACHE.pop(client_ip, None)
            except Exception:
                pass
                
        return response
        
    return await handler(request)

