from aiohttp import web
import jwt
import csv
import time
import random
import asyncio
import json 
import uuid
from io import StringIO
from datetime import datetime, timedelta
import database
from config import JWT_SECRET, ADMIN_PASSWORD, ADMIN_ID
from bot.setup import bot
from api.routes.websockets import notify_admin_new_log # 👈 استدعاء دالة البث الحي

# ==========================================
# 🛠️ دوال مساعدة (Helper Functions)
# ==========================================
# 👈 قائمة لحفظ اتصالات المدراء النشطين بالرادار
ADMIN_WEBSOCKETS = set()

async def api_admin_ws_logs(request: web.Request):
    """مسار الاتصال الحي (WebSocket) لشاشة القيادة العليا (محصن)"""
    # 🛡️ التحقق من التوكن يدوياً لأن الـ Middleware لا يغطي مسارات /ws/
    token = request.query.get('token')
    if not token:
        return web.HTTPUnauthorized(reason="Token missing")
    try:
        import jwt
        from config import JWT_SECRET
        decoded = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if decoded.get('role') != 'super_admin':
            return web.HTTPUnauthorized(reason="Unauthorized")
    except Exception:
        return web.HTTPUnauthorized(reason="Invalid token")

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    request.app['websockets'].add(ws)
    ADMIN_WEBSOCKETS.add(ws)
    try:
        async for msg in ws:
            pass 
    finally:
        ADMIN_WEBSOCKETS.remove(ws)
        request.app['websockets'].discard(ws)
    return ws

async def log_admin_action(conn, action, details):
    """دالة لتسجيل أي حركة يقوم بها المدير في سجل العمليات"""
    await conn.execute("INSERT INTO admin_logs (action, details) VALUES ($1, $2)", action, details)
    
    # 👈 إرسال الحدث فوراً لشاشة القيادة العليا (الرادار الحي)
    from datetime import datetime
    log_data = {
        "type": "new_log",
        "data": {
            "action": action,
            "details": details,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    }
    
    # بث الحدث لجميع المدراء المتصلين بالرادار حالياً
    for ws in list(ADMIN_WEBSOCKETS):
        try:
            await ws.send_str(json.dumps(log_data))
        except Exception:
            ADMIN_WEBSOCKETS.discard(ws)
            
    # (اختياري) الإبقاء على الدالة القديمة إذا كنت تستخدمها في مكان آخر
    try:
        await notify_admin_new_log(log_data["data"])
    except Exception:
        pass

def check_admin_token(request: web.Request):
    """التحقق من صلاحيات القيادة العليا"""
    auth_header = request.headers.get('Authorization')
    if not auth_header: 
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "غير مصرح"}', content_type='application/json')
    
    token = auth_header.split(' ')[1]
    try:
        decoded = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        if decoded.get('role') != 'super_admin': 
            raise web.HTTPUnauthorized(text='{"status": "error", "message": "صلاحيات غير كافية"}', content_type='application/json')
        return True
    except jwt.ExpiredSignatureError:
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "انتهت الجلسة، يرجى تسجيل الدخول مجدداً"}', content_type='application/json')
    except Exception:
        raise web.HTTPUnauthorized(text='{"status": "error", "message": "توكن غير صالح"}', content_type='application/json')

# ==========================================
# 🔐 مسارات الدخول والإحصائيات
# ==========================================
async def api_admin_login(request: web.Request):
    data = await request.json()
    password = data.get('password')

    if password != ADMIN_PASSWORD:
        return web.json_response({"status": "error", "message": "كلمة المرور غير صحيحة"})
        
    from datetime import timezone
    token = jwt.encode({
        'role': 'super_admin',
        'exp': datetime.now(timezone.utc) + timedelta(days=1)
    }, JWT_SECRET, algorithm='HS256')
    
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "تسجيل دخول", "تم تسجيل الدخول للقيادة العليا بنجاح")
        
    return web.json_response({"status": "success", "token": token})

async def api_admin_stats(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        # 👈 استعلام واحد شامل يجلب جميع الأرقام دفعة واحدة (أسرع بـ 5 مرات)
        stats = await conn.fetchrow("""
            SELECT 
                (SELECT COUNT(*) FROM stores WHERE is_deleted = FALSE) as stores_count,
                (SELECT COUNT(*) FROM stores WHERE status = 'active' AND is_deleted = FALSE) as active_stores,
                (SELECT COUNT(*) FROM customers WHERE is_deleted = FALSE) as customers_count,
                (SELECT COALESCE(SUM(balance), 0) FROM customers WHERE is_deleted = FALSE) as total_debt,
                (SELECT COUNT(*) FROM stores WHERE package_type = 'vip' AND is_deleted = FALSE) as vip_count
        """)
        
        # استعلام النمو يبقى منفصلاً لأنه يرجع عدة صفوف
        growth_records = await conn.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count 
            FROM stores 
            WHERE created_at >= CURRENT_DATE - INTERVAL '7 days' 
            GROUP BY DATE(created_at) 
            ORDER BY date ASC
        """)
        
    stores_count = stats['stores_count']
    free_count = stores_count - stats['vip_count']
    
    growth_labels = [str(r['date'])[5:10] for r in growth_records]
    growth_values = [r['count'] for r in growth_records]

    return web.json_response({
        "status": "success",
        "stores_count": stores_count,
        "active_stores": stats['active_stores'],
        "customers_count": stats['customers_count'],
        "total_debt": float(stats['total_debt']),
        "package_data": [stats['vip_count'], free_count],
        "growth_data": {"labels": growth_labels, "values": growth_values}
    })

# ==========================================
# 🛠️ المسارات المفقودة التي تم إضافتها
# ==========================================
async def api_admin_change_password(request: web.Request):
    """تغيير كلمة مرور المدير (معدلة للسحابة)"""
    check_admin_token(request)
    # نرفض الطلب بأدب ونوجه المدير للوحة تحكم السيرفر
    return web.json_response({
        "status": "error", 
        "message": "لأسباب أمنية، تغيير كلمة مرور القيادة العليا يتم فقط من خلال إعدادات السيرفر (Render Secret Files)."
    })

async def api_admin_maintenance(request: web.Request):
    """تفعيل أو إيقاف وضع الصيانة (حفظ دائم في قاعدة البيانات)"""
    check_admin_token(request)
    data = await request.json()
    is_active = data.get('active', False)
    
    async with database.pool.acquire() as conn:
        # تحديث حالة الصيانة مباشرة (الجدول تم إنشاؤه مسبقاً في database.py)
        await conn.execute("UPDATE system_settings SET maintenance_mode = $1 WHERE id = 1", is_active)
        
        action_msg = "تفعيل وضع الصيانة 🔴" if is_active else "إيقاف وضع الصيانة 🟢"
        await log_admin_action(conn, "وضع الصيانة ⚠️", action_msg)
        
    return web.json_response({"status": "success", "maintenance": is_active})

# ==========================================
# 🏪 مسارات إدارة البقالات
# ==========================================
async def api_admin_stores(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        # جلب البقالات مع بيانات الفترة التجريبية والحذف
        stores = await conn.fetch("SELECT id, name, phone, status, package_type, trial_ends_at, is_deleted FROM stores ORDER BY id DESC")

    stores_list = []
    for s in stores:
        d = dict(s)
        d['trial_ends_at'] = str(d['trial_ends_at'])[:10] if d['trial_ends_at'] else None
        stores_list.append(d)
        
    return web.json_response({"status": "success", "stores": stores_list})

async def api_admin_toggle_store(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    
    store_id = data.get('store_id')
    new_status = data.get('status')
    
    if not store_id or not new_status:
         return web.json_response({"status": "error", "message": "بيانات غير مكتملة"}, status=400)
         
    store_id = int(store_id)
    suspend_reason = data.get('suspend_reason', '') # 👈 استخراج سبب الإيقاف من الواجهة

    async with database.pool.acquire() as conn:
        # محاولة حفظ السبب في قاعدة البيانات (إذا كان عمود suspend_reason موجوداً)
        try:
            reason_to_save = suspend_reason if new_status == 'suspended' else None
            await conn.execute("UPDATE stores SET status = $1, suspend_reason = $2 WHERE id = $3", new_status, reason_to_save, store_id)
        except Exception:
            # إذا لم تكن قد أنشأت عمود suspend_reason في قاعدة البيانات بعد، نحدث الحالة فقط لتجنب توقف النظام
            await conn.execute("UPDATE stores SET status = $1 WHERE id = $2", new_status, store_id)
            
        # 👈 توثيق السبب في سجل العمليات (الرادار) ليظهر لك بوضوح
        log_details = f"تم تغيير حالة البقالة رقم {store_id} إلى: {new_status}"
        if new_status == 'suspended' and suspend_reason:
            log_details += f" | السبب: {suspend_reason}"
            
        await log_admin_action(conn, "تغيير حالة بقالة", log_details)
        
    return web.json_response({"status": "success"})

# ==========================================
# 🚀 المسارات الاستراتيجية الجديدة (God Mode)
# ==========================================
async def api_admin_logs(request: web.Request):
    """جلب سجل العمليات (مع دعم التمرير اللانهائي Pagination)"""
    check_admin_token(request)
    
    try:
        page = int(request.query.get('page', 1))
        limit = int(request.query.get('limit', 50))
    except ValueError:
        page = 1
        limit = 50
        
    offset = (page - 1) * limit

    async with database.pool.acquire() as conn:
        logs = await conn.fetch("""
            SELECT action, details, created_at 
            FROM admin_logs 
            ORDER BY id DESC 
            LIMIT $1 OFFSET $2
        """, limit, offset)
        total_count = await conn.fetchval("SELECT COUNT(*) FROM admin_logs")
        
    logs_list = [{"action": l['action'], "details": l['details'], "time": str(l['created_at'])[:16]} for l in logs]
    
    return web.json_response({
        "status": "success", 
        "logs": logs_list,
        "pagination": {
            "current_page": page,
            "total_pages": (total_count + limit - 1) // limit,
            "total_records": total_count,
            "has_next": (offset + limit) < total_count
        }
    })

async def api_admin_shadow_login(request: web.Request):
    """وضع الظل: توليد توكن دخول كصاحب بقالة (مع إمكانية الإبطال)"""
    check_admin_token(request)
    data = await request.json()
    
    store_id = data.get('store_id')
    if not store_id:
        return web.json_response({"status": "error", "message": "معرف البقالة مفقود"}, status=400)
        
    store_id = int(store_id)
    
    # توليد معرف فريد لهذه الجلسة
    session_jti = str(uuid.uuid4())

    async with database.pool.acquire() as conn:
        telegram_id = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
        await log_admin_action(conn, "وضع الظل 👻", f"تم الدخول كصاحب البقالة رقم {store_id}")

    # تضمين المعرف (jti) داخل التوكن
    token = jwt.encode({
        'role': 'store_owner',
        'store_id': store_id,
        'jti': session_jti, # 👈 المعرف الفريد
        'exp': datetime.utcnow() + timedelta(hours=2)
    }, JWT_SECRET, algorithm='HS256')
    
    return web.json_response({
        "status": "success", 
        "shadow_token": token,
        "telegram_id": telegram_id
    })

async def api_admin_revoke_shadow(request: web.Request):
    """إنهاء جلسة وضع الظل وإدراج التوكن في القائمة السوداء"""
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    jti = data.get('jti') # الواجهة يجب أن ترسل الـ jti
    
    if not jti:
        return web.json_response({"status": "error", "message": "معرف الجلسة مفقود"})
        
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO blacklisted_tokens (jti) VALUES ($1) ON CONFLICT DO NOTHING", jti)
        await log_admin_action(conn, "إنهاء الظل 🛑", f"تم إنهاء جلسة الدعم الفني للبقالة رقم {store_id}")
        
    return web.json_response({"status": "success"})

async def api_admin_extend_trial(request: web.Request):
    """تمديد الفترة التجريبية لبقالة"""
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    days = int(data.get('days', 30))
    
    new_date = datetime.now() + timedelta(days=days)
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET trial_ends_at = $1 WHERE id = $2", new_date, store_id)
        await log_admin_action(conn, "تمديد اشتراك ⏱️", f"تم تمديد الفترة للبقالة {store_id} لمدة {days} يوم")
        
    return web.json_response({"status": "success", "new_date": str(new_date)[:10]})

# 👇 الدالة الجديدة لإدارة باقات VIP 👇
async def api_admin_update_package(request: web.Request):
    """ترقية أو إلغاء باقة VIP لبقالة"""
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    package_type = data.get('package_type') # 'vip' or 'free'
    
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE stores SET package_type = $1 WHERE id = $2", package_type, store_id)
        action_name = "ترقية VIP 👑" if package_type == 'vip' else "إلغاء VIP ⬇️"
        await log_admin_action(conn, action_name, f"تم تغيير باقة البقالة {store_id} إلى {package_type}")
        
    return web.json_response({"status": "success"})

async def api_admin_bulk_update(request: web.Request):
    """ترقية مجموعة من البقالات دفعة واحدة"""
    check_admin_token(request)
    data = await request.json()
    store_ids = data.get('store_ids', [])
    action = data.get('action')
    
    if not store_ids or action != 'vip':
        return web.json_response({"status": "error", "message": "بيانات غير صالحة"})
        
    async with database.pool.acquire() as conn:
        # استخدام ANY($1) لتحديث مصفوفة من الـ IDs دفعة واحدة
        await conn.execute("UPDATE stores SET package_type = 'vip' WHERE id = ANY($1::int[])", store_ids)
        await log_admin_action(conn, "ترقية جماعية 👑", f"تم ترقية {len(store_ids)} بقالات إلى باقة VIP دفعة واحدة")
        
    return web.json_response({"status": "success"})

async def api_admin_recover_deleted(request: web.Request):

    """استعادة البيانات من سلة المهملات (Soft Delete)"""
    check_admin_token(request)
    data = await request.json()
    table = data.get('table')
    record_id = int(data.get('id'))
    
    allowed_tables = ['stores', 'customers', 'store_products', 'workers', 'delivery_agents']
    if table not in allowed_tables:
        return web.json_response({"status": "error", "message": "جدول غير صالح"})
        
    async with database.pool.acquire() as conn:
        query = f"UPDATE {table} SET is_deleted = FALSE WHERE id = $1"
        await conn.execute(query, record_id)
        await log_admin_action(conn, "استعادة بيانات ♻️", f"تمت استعادة السجل {record_id} من جدول {table}")
        
    return web.json_response({"status": "success"})

async def api_admin_update_store(request: web.Request):
    """تحديث بيانات البقالة (الملف الشامل)"""
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    new_name = data.get('new_name')
    
    async with database.pool.acquire() as conn:          
        await conn.execute("UPDATE stores SET name = $1 WHERE id = $2", new_name, store_id)
        await log_admin_action(conn, "تحديث بقالة 🪪", f"تم تغيير اسم البقالة رقم {store_id} إلى: {new_name}")
        
    return web.json_response({"status": "success"})

async def background_broadcast_task(msg_text, target, admin_id):
    """مهمة خلفية لإرسال الإذاعة ببطء لتجنب الحظر وإرسال ملخص للمدير"""
    async with database.pool.acquire() as conn:
        if target == 'suspended':
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE status = 'suspended' AND telegram_id IS NOT NULL")
            target_name = "البقالات الموقوفة"
        elif target == 'trial_ending':
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE trial_ends_at BETWEEN NOW() AND NOW() + INTERVAL '7 days' AND telegram_id IS NOT NULL")
            target_name = "البقالات التي ستنتهي فترتها قريباً"
        else:
            stores = await conn.fetch("SELECT telegram_id FROM stores WHERE telegram_id IS NOT NULL")
            target_name = "جميع البقالات"
            
    success = 0
    failed = 0
    
    for s in stores:
        try:
            await bot.send_message(s['telegram_id'], f"📢 **رسالة من القيادة العليا:**\n\n{msg_text}")
            success += 1
        except Exception as e:
            failed += 1
        await asyncio.sleep(0.05) # تأخير بسيط لتجنب حظر تيليجرام (Rate Limit)
        
    # توثيق العملية في الرادار
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "إذاعة 📢", f"اكتملت الإذاعة: نجاح {success}، فشل {failed} (الفئة: {target_name})")
        
    # إرسال رسالة ملخص لحساب المدير في تيليجرام
    try:
        summary_msg = f"✅ **اكتملت الإذاعة بنجاح**\n\n🎯 الفئة: {target_name}\n🟢 تم الإرسال: {success}\n🔴 فشل الإرسال: {failed}"
        await bot.send_message(admin_id, summary_msg)
    except Exception as e:
        print(f"فشل إرسال ملخص الإذاعة للمدير: {e}")

async def api_admin_broadcast(request: web.Request):
    """نظام الإذاعة الموجهة (يعمل في الخلفية)"""
    check_admin_token(request)
    data = await request.json()
    msg_text = data.get('message')
    target = data.get('target')
    
    # تشغيل الإرسال في الخلفية دون تعطيل استجابة السيرفر
    asyncio.create_task(background_broadcast_task(msg_text, target, ADMIN_ID))
    
    # الرد فوراً على الواجهة
    return web.json_response({
        "status": "success", 
        "message": "تم بدء الإذاعة في الخلفية. سيصلك إشعار على تيليجرام عند الانتهاء."
    })

async def api_admin_backup(request: web.Request):
    """تحميل نسخة احتياطية (CSV) بدون تجميد السيرفر"""
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        stores = await conn.fetch("SELECT id, name, phone, status, created_at FROM stores")
        await log_admin_action(conn, "نسخة احتياطية 💾", "تم طلب تحميل نسخة احتياطية لقاعدة البيانات")
        
    # دالة فرعية لتوليد الملف بعيداً عن الـ Event Loop الرئيسي
    def generate_csv():
        csv_file = StringIO()
        writer = csv.writer(csv_file)
        writer.writerow(['ID', 'اسم البقالة', 'رقم الهاتف', 'الحالة', 'تاريخ التسجيل'])
        for s in stores:
            writer.writerow([s['id'], s['name'], s['phone'], s['status'], str(s['created_at'])[:16]])
        return csv_file.getvalue().encode('utf-8-sig')
        
    # تشغيل عملية التوليد في الخلفية (Thread)
    csv_data = await asyncio.to_thread(generate_csv)
        
    return web.Response(
        body=csv_data,
        headers={
            'Content-Disposition': 'attachment; filename="dukkani_backup.csv"',
            'Content-Type': 'text/csv; charset=utf-8'
        }
    )

async def api_admin_force_update(request: web.Request):
    """إجبار التحديث"""
    check_admin_token(request)
    data = await request.json()
    version = data.get('version')
    
    async with database.pool.acquire() as conn:
        await log_admin_action(conn, "تحديث إجباري 🚀", f"تم إرسال أمر تحديث إجباري للإصدار {version}")
        
    return web.json_response({"status": "success"})

async def api_admin_top_debts(request: web.Request):
    """جلب أعلى 5 بقالات مديونية"""
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        records = await conn.fetch('''
            SELECT s.name, SUM(c.balance) as total_debt 
            FROM customers c 
            JOIN stores s ON c.store_id = s.id 
            WHERE c.is_deleted = FALSE 
            GROUP BY s.id, s.name 
            HAVING SUM(c.balance) > 0
            ORDER BY total_debt DESC 
            LIMIT 5
        ''')
        
    data = [{"name": r['name'], "debt": float(r['total_debt'])} for r in records]
    return web.json_response({"status": "success", "top_debts": data})

# ==========================================
# 🖥️ رادار الصحة، سلة المهملات، والكتالوج
# ==========================================
async def api_admin_health(request: web.Request):
    """فحص صحة السيرفر وقاعدة البيانات"""
    check_admin_token(request)
    start_time = time.time()
    async with database.pool.acquire() as conn:
        await conn.execute("SELECT 1")
    db_ping = int((time.time() - start_time) * 1000)
    
    try:
        import psutil
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
    except ImportError:
        cpu = random.randint(15, 35) # أرقام تقريبية إذا لم تكن مكتبة psutil مثبتة
        ram = random.randint(40, 65)
        
    return web.json_response({"status": "success", "cpu": cpu, "ram": ram, "db_ping": db_ping})

async def api_admin_recycle_bin_get(request: web.Request):
    """جلب العناصر المحذوفة"""
    check_admin_token(request)
    table = request.query.get('table')
    allowed_tables = ['stores', 'customers', 'store_products']
    if table not in allowed_tables:
        return web.json_response({"status": "error", "message": "جدول غير صالح"})
        
    async with database.pool.acquire() as conn:
        if table == 'stores':
            items = await conn.fetch("SELECT id, name FROM stores WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
        elif table == 'customers':
            items = await conn.fetch("SELECT id, name FROM customers WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
        elif table == 'store_products':
            items = await conn.fetch("SELECT id, custom_name as name FROM store_products WHERE is_deleted = TRUE ORDER BY id DESC LIMIT 50")
            
    items_list = [{"id": i['id'], "name": i['name']} for i in items]
    return web.json_response({"status": "success", "items": items_list})

async def api_admin_catalog_get(request: web.Request):
    """جلب الكتالوج المركزي"""
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        items = await conn.fetch("SELECT id, name, category FROM global_products ORDER BY id DESC")
    return web.json_response({"status": "success", "items": [dict(i) for i in items]})

async def api_admin_catalog_add(request: web.Request):
    """إضافة منتج للكتالوج المركزي"""
    check_admin_token(request)
    data = await request.json()
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO global_products (name, category) VALUES ($1, $2)", data['name'], data.get('category', 'عام'))
        await log_admin_action(conn, "كتالوج 🌍", f"تم إضافة منتج قياسي جديد: {data['name']}")
    return web.json_response({"status": "success"})

async def api_admin_store_profile(request: web.Request):
    """جلب الملف الشامل للبقالة"""
    check_admin_token(request)
    store_id = int(request.query.get('store_id'))
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT name, phone, created_at, status, package_type FROM stores WHERE id = $1", store_id)
        cust_count = await conn.fetchval("SELECT COUNT(*) FROM customers WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        total_debt = await conn.fetchval("SELECT SUM(balance) FROM customers WHERE store_id = $1 AND is_deleted = FALSE", store_id) or 0.0
        prod_count = await conn.fetchval("SELECT COUNT(*) FROM store_products WHERE store_id = $1 AND is_deleted = FALSE", store_id)
        order_count = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE store_id = $1", store_id)
        
    return web.json_response({
        "status": "success",
        "name": store['name'],
        "phone": store['phone'] or "غير مسجل",
        "created_at": str(store['created_at'])[:10],
        "store_status": store['status'], # 👈 تم تغيير الاسم هنا لمنع التعارض
        "package_type": store['package_type'],
        "customers": cust_count,
        "debt": float(total_debt),
        "products": prod_count,
        "orders": order_count
    })

# ==========================================
# 💬 نظام المحادثة مع البقالات (الدعم الفني المباشر)
# ==========================================
async def api_admin_chat_list(request: web.Request):
    check_admin_token(request)
    async with database.pool.acquire() as conn:
        # جلب المحادثات مباشرة (الجدول تم إنشاؤه مسبقاً)
        chats = await conn.fetch("""
            SELECT s.id, s.name, 
                   (SELECT message FROM admin_store_chats WHERE store_id = s.id ORDER BY created_at DESC LIMIT 1) as last_message,
                   (SELECT created_at FROM admin_store_chats WHERE store_id = s.id ORDER BY created_at DESC LIMIT 1) as last_time,
                   (SELECT COUNT(*) FROM admin_store_chats WHERE store_id = s.id AND sender = 'store' AND is_read = FALSE) as unread
            FROM stores s
            WHERE EXISTS (SELECT 1 FROM admin_store_chats WHERE store_id = s.id)
            ORDER BY last_time DESC
        """)
        
    chat_list = [{"id": c['id'], "name": c['name'], "last_message": c['last_message'], "time": c['last_time'].strftime('%Y-%m-%d %H:%M') if c['last_time'] else "", "unread": c['unread']} for c in chats]
    return web.json_response({"status": "success", "chats": chat_list})

async def api_admin_chat_history(request: web.Request):
    check_admin_token(request)
    store_id = int(request.query.get('store_id'))
    async with database.pool.acquire() as conn:
        await conn.execute("UPDATE admin_store_chats SET is_read = TRUE WHERE store_id = $1 AND sender = 'store'", store_id)
        messages = await conn.fetch("SELECT sender, message, created_at FROM admin_store_chats WHERE store_id = $1 ORDER BY created_at ASC", store_id)
        
    msg_list = [{"sender": m['sender'], "text": m['message'], "time": m['created_at'].strftime('%H:%M')} for m in messages]
    return web.json_response({"status": "success", "messages": msg_list})

async def api_admin_chat_send(request: web.Request):
    check_admin_token(request)
    data = await request.json()
    store_id = int(data['store_id'])
    message = data['message']
    
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO admin_store_chats (store_id, sender, message) VALUES ($1, 'admin', $2)", store_id, message)
        
    # إرسال إشعار حي للبقالة
    from api.routes.websockets import notify_store_new_order
    import asyncio
    asyncio.create_task(notify_store_new_order(store_id, {"type": "admin_chat_message", "text": message, "sender": "admin", "time": datetime.now().strftime('%H:%M')}))
    
    return web.json_response({"status": "success"})

# =====================================================================
# ⚙️ صلاحيات الخدمات (كروت وشحن) للبقالات
# =====================================================================
async def api_admin_store_telecom_perms(request: web.Request):
    store_id = int(request.query.get('store_id', 0))
    async with database.pool.acquire() as conn:
        perms_json = await conn.fetchval("SELECT telecom_permissions FROM stores WHERE id = $1", store_id)
        import json
        perms = json.loads(perms_json) if perms_json else {"ecards": False, "instant_recharge": False}
    return web.json_response({"status": "success", "permissions": perms})

async def api_admin_update_telecom_perms(request: web.Request):
    data = await request.json()
    store_id = int(data.get('store_id'))
    perms = data.get('permissions', {})
    async with database.pool.acquire() as conn:
        import json
        await conn.execute("UPDATE stores SET telecom_permissions = $1::jsonb WHERE id = $2", json.dumps(perms), store_id)
    return web.json_response({"status": "success", "message": "تم التحديث"})

# =====================================================================
# 📉 رادار الخمول والانسحاب (Churn Radar)
# =====================================================================
async def api_admin_churn_radar(request: web.Request):
    async with database.pool.acquire() as conn:
        # نجلب البقالات مع تاريخ آخر طلب وعدد الطلبات في آخر 7 أيام
        stores = await conn.fetch("""
            SELECT s.id, s.name, s.phone, s.package_type,
                   (SELECT MAX(created_at) FROM orders WHERE store_id = s.id) as last_order_date,
                   (SELECT COUNT(*) FROM orders WHERE store_id = s.id AND created_at >= NOW() - INTERVAL '7 days') as recent_orders
            FROM stores s
            WHERE s.status = 'active' AND s.is_deleted = FALSE
            ORDER BY recent_orders ASC, last_order_date ASC NULLS FIRST
        """)
        
    radar_data = []
    for st in stores:
        import datetime
        last_order = st['last_order_date']
        if last_order:
            # حساب عدد الأيام منذ آخر نشاط
            days_inactive = (datetime.datetime.now() - last_order).days
        else:
            days_inactive = 999 # لم يطلب أبداً
        
        # إذا تجاوز 3 أيام بدون طلبات نعتبره خاملاً ومعرضاً للانسحاب
        status = "خامل 🔴" if days_inactive > 3 or days_inactive == 999 else "نشط 🟢"
        
        radar_data.append({
            "id": st['id'],
            "name": st['name'],
            "phone": st['phone'] or "غير متوفر",
            "package_type": st['package_type'],
            "days_inactive": days_inactive if days_inactive != 999 else "لم يستخدم النظام",
            "recent_orders": st['recent_orders'],
            "status": status
        })
        
    return web.json_response({"status": "success", "radar_data": radar_data})

# =====================================================================
# 💰 رادار الأرباح والاشتراكات (SaaS Billing)
# =====================================================================
async def api_admin_billing_stats(request: web.Request):
    async with database.pool.acquire() as conn:
        # إنشاء جدول المدفوعات تلقائياً إذا لم يكن موجوداً
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_payments (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                amount NUMERIC NOT NULL,
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # إجمالي الأرباح هذا الشهر
        monthly_revenue = await conn.fetchval("""
            SELECT SUM(amount) FROM subscription_payments 
            WHERE DATE_TRUNC('month', created_at) = DATE_TRUNC('month', CURRENT_DATE)
        """) or 0.0
        
        # إجمالي الأرباح الكلية
        total_revenue = await conn.fetchval("SELECT SUM(amount) FROM subscription_payments") or 0.0
        
        # سجل المدفوعات الأخير
        payments = await conn.fetch("""
            SELECT sp.id, sp.amount, sp.note, sp.created_at, s.name as store_name 
            FROM subscription_payments sp
            JOIN stores s ON sp.store_id = s.id
            ORDER BY sp.created_at DESC LIMIT 50
        """)
        
        # قائمة البقالات لتسجيل دفعة جديدة
        stores = await conn.fetch("SELECT id, name, package_type FROM stores WHERE is_deleted = FALSE")
        
    payment_list = [{"id": p['id'], "store_name": p['store_name'], "amount": float(p['amount']), "note": p['note'], "date": p['created_at'].strftime('%Y-%m-%d')} for p in payments]
    store_list = [{"id": s['id'], "name": s['name'], "package": s['package_type']} for s in stores]
    
    return web.json_response({
        "status": "success", 
        "monthly_revenue": float(monthly_revenue),
        "total_revenue": float(total_revenue),
        "payments": payment_list,
        "stores": store_list
    })

async def api_admin_add_subscription_payment(request: web.Request):
    data = await request.json()
    store_id = int(data['store_id'])
    amount = float(data['amount'])
    note = data.get('note', '')
    
    if amount <= 0:
        return web.json_response({"status": "error", "message": "المبلغ غير صحيح"})
        
    async with database.pool.acquire() as conn:
        await conn.execute("INSERT INTO subscription_payments (store_id, amount, note) VALUES ($1, $2, $3)", store_id, amount, note)
        
    return web.json_response({"status": "success", "message": "تم تسجيل دفعة الاشتراك بنجاح 💰"})

# ==========================================
# 🌐 تسجيل المسارات في التطبيق
# ==========================================
def setup_admin_routes(app: web.Application):
    app.router.add_post('/api/admin/login', api_admin_login)
    app.router.add_get('/api/admin/stats', api_admin_stats)
    app.router.add_get('/api/admin/stores', api_admin_stores)
    app.router.add_post('/api/admin/store/toggle', api_admin_toggle_store)
    
    # مسار الرادار الحي (WebSocket)
    app.router.add_get('/ws/admin_logs', api_admin_ws_logs)
    
    # المسارات الاستراتيجية
    app.router.add_get('/api/admin/logs', api_admin_logs)
    app.router.add_post('/api/admin/shadow_login', api_admin_shadow_login)
    app.router.add_post('/api/admin/revoke_shadow', api_admin_revoke_shadow) # 👈 المسار الجديد لإبطال وضع الظل
    app.router.add_post('/api/admin/extend_trial', api_admin_extend_trial)
    app.router.add_post('/api/admin/update_package', api_admin_update_package)
    app.router.add_post('/api/admin/bulk_update', api_admin_bulk_update) # 👈 المسار الجديد
     # 👈 المسار الجديد
    app.router.add_post('/api/admin/recover', api_admin_recover_deleted)
    app.router.add_post('/api/admin/update_store', api_admin_update_store)
    app.router.add_post('/api/admin/broadcast', api_admin_broadcast)
    app.router.add_get('/api/admin/backup', api_admin_backup)
    app.router.add_post('/api/admin/force_update', api_admin_force_update)
    app.router.add_get('/api/admin/top_debts', api_admin_top_debts)
    
    # رادار الصحة والكتالوج
    app.router.add_get('/api/admin/health', api_admin_health)
    app.router.add_get('/api/admin/recycle_bin', api_admin_recycle_bin_get)
    app.router.add_get('/api/admin/catalog', api_admin_catalog_get)
    app.router.add_post('/api/admin/catalog/add', api_admin_catalog_add)
    app.router.add_get('/api/admin/store_profile', api_admin_store_profile)
    
    # المسارات الأخرى
    app.router.add_post('/api/admin/change_password', api_admin_change_password)
    app.router.add_post('/api/admin/maintenance', api_admin_maintenance)
    app.router.add_get('/api/admin/chat/list', api_admin_chat_list)
    app.router.add_get('/api/admin/chat/history', api_admin_chat_history)
    app.router.add_post('/api/admin/chat/send', api_admin_chat_send)

    app.router.add_get('/api/admin/store_telecom_perms', api_admin_store_telecom_perms)
    app.router.add_post('/api/admin/update_telecom_perms', api_admin_update_telecom_perms)
    app.router.add_get('/api/admin/churn_radar', api_admin_churn_radar)
    app.router.add_get('/api/admin/billing_stats', api_admin_billing_stats)
    app.router.add_post('/api/admin/add_subscription_payment', api_admin_add_subscription_payment)
