# مسار الملف: api/routes/pos.py
from aiohttp import web
import json
import logging
import asyncio
import time # 👈 استيراد مكتبة الوقت
import database
from services.notifications import send_push_notification
from bot.setup import bot # 👈 استدعاء البوت لإرسال الإنذارات
from config import ADMIN_ID # 👈 استدعاء آيدي القيادة العليا

# 👇 قاموس لحفظ وقت آخر طلب لكل بقالة لمنع التكرار 👇
POS_ORDER_COOLDOWN = {}

async def api_pos_data(request: web.Request ):
    store_tid = request.query.get('store_id')
    if not store_tid or store_tid == 'NaN' or store_tid == 'undefined' or not str(store_tid).isdigit(): 
        return web.json_response({"error": "Missing or invalid store_id"}, status=400)

    async with database.pool.acquire() as conn:
        # 👇 التعديل السحري: البحث بالتيليجرام أو الآيدي الداخلي ليدعم وضع الظل 👇
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1 OR id = $1", int(store_tid))
        if not store_id:
            # التحقق مما إذا كان عاملاً
            store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", int(store_tid))
            
        if not store_id: return web.json_response({"error": "Store not found"}, status=404)
        
        # ✅ التعديل هنا: جلب الصورة من البقالة، وإذا لم توجد يأخذها من الكتالوج العام (مع تجاهل المحذوف)
        products = await conn.fetch("""
            SELECT sp.id, sp.custom_name as name, sp.price, sp.category, sp.barcode, 
                   sp.carton_qty, sp.carton_price, sp.carton_barcode,
                   COALESCE(sp.telegram_file_id, gp.image_url) as telegram_file_id 
            FROM store_products sp 
            LEFT JOIN global_products gp ON sp.product_id = gp.id 
            WHERE sp.store_id = $1 AND sp.is_available=TRUE AND sp.is_deleted = FALSE
        """, store_id)
        
        # جلب الزبائن غير المحذوفين (وإخفاء الحسابات الفرعية للعائلة)
        customers = await conn.fetch("SELECT id, name, balance FROM customers WHERE store_id = $1 AND is_deleted = FALSE AND parent_id IS NULL", store_id)
        
    prod_list = [{"id": p['id'], "name": p['name'], "price": float(p['price']), "category": p['category'] or 'عام', "barcode": p['barcode'], "image": p['telegram_file_id'], "carton_qty": p['carton_qty'], "carton_price": float(p['carton_price']), "carton_barcode": p['carton_barcode']} for p in products]
    cust_list = [{"id": c['id'], "name": c['name'], "balance": float(c['balance'])} for c in customers]
        
    async with database.pool.acquire() as conn:
        store_info = await conn.fetchrow("SELECT theme_color, logo_url FROM stores WHERE id = $1", store_id)
        
    return web.json_response({
        "theme_color": store_info['theme_color'] if store_info else '#0ba360',
        "products": prod_list,
        "customers": cust_list
    })

async def api_pos_order(request: web.Request):
    """استقبال طلبات الكاشير السريع (طلبات جديدة أو مرتجعات)"""
    try:
        data = await request.json()
        
        # 🛡️ حماية من البيانات الناقصة
        if 'store_id' not in data or 'customer_id' not in data or 'items' not in data:
            return web.json_response({"status": "error", "message": "بيانات الطلب غير مكتملة."})
            
        store_tid = int(data['store_id'])
        worker_id = data.get('worker_id')
        if worker_id: worker_id = int(worker_id)
        
        cid = int(data['customer_id'])
        discount = float(data.get('discount', 0.0))
        is_return = data.get('is_return', False)
        items = data['items']
        
        if not items:
            return web.json_response({"status": "error", "message": "السلة فارغة."})

        async with database.pool.acquire() as conn:
            store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", store_tid)
            if not store_id:
                store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", store_tid)
                
            if not store_id:
                return web.json_response({"status": "error", "message": "لم يتم العثور على البقالة."})

            # حماية من الطلبات المكررة
            current_time = time.time()
            cooldown_key = f"{store_id}_{worker_id}"
            if cooldown_key in POS_ORDER_COOLDOWN:
                if current_time - POS_ORDER_COOLDOWN[cooldown_key] < 3:
                    return web.json_response({"status": "error", "message": "الرجاء الانتظار لحظة قبل إرسال طلب جديد."})
            POS_ORDER_COOLDOWN[cooldown_key] = current_time

            # 🛡️ 1. إعادة حساب الإجمالي من قاعدة البيانات (لمنع التلاعب)
            real_total = 0.0
            for pid_str, item in items.items():
                qty = int(item.get('qty', 1))
                if qty <= 0:
                    return web.json_response({"status": "error", "message": "الكمية يجب أن تكون أكبر من الصفر."})
                    
                db_price = await conn.fetchval("SELECT price FROM store_products WHERE id = $1 AND store_id = $2", int(pid_str), store_id)
                if not db_price:
                    return web.json_response({"status": "error", "message": f"المنتج رقم {pid_str} غير موجود."})
                    
                real_total += float(db_price) * qty
                
            # تطبيق الخصم
            final_total = real_total - discount
            if final_total < 0 and not is_return: final_total = 0

            # 2. فحص سقف الدين (مع قفل الصف FOR UPDATE لمنع التضارب)
            if cid != 0:
                cust = await conn.fetchrow("SELECT name, balance, credit_limit FROM customers WHERE id = $1 FOR UPDATE", cid)
                if not cust:
                    return web.json_response({"status": "error", "message": "الزبون غير موجود."})
                    
                if not is_return and (float(cust['balance']) + final_total > float(cust['credit_limit'])):
                    return web.json_response({"status": "error", "message": f"الطلب يتجاوز سقف الدين المسموح للزبون ({cust['name']})."})

            # 3. تحديث المخزون (مع منع المخزون السالب)
            for pid_str, item in items.items():
                qty = int(item.get('qty', 1))
                if is_return:
                    await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", qty, int(pid_str))
                else:
                    res = await conn.execute("UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND (stock IS NULL OR stock >= $1)", qty, int(pid_str))
                    if res == "UPDATE 0":
                        return web.json_response({"status": "error", "message": f"الكمية المطلوبة من المنتج غير متوفرة في المخزون."})

            # 4. تجهيز التفاصيل وتسجيل المعاملة
            details = "\n".join([f"- {item['qty']}x {item['name']}" for item in items.values()])
            if discount > 0:
                details += f"\n🎁 الخصم: {discount} ريال"
                
            trans_type = 'pos_return' if is_return else 'pos_order'
            action_name = "مرتجع" if is_return else "طلب"
            
            if cid != 0:
                details_full = f"{action_name} كاشير (آجل)\n{details}\nالإجمالي النهائي: {final_total}"
                
                if is_return:
                    await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", final_total, cid)
                    await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, cid, trans_type, -final_total, details_full)
                    new_balance = float(cust['balance']) - final_total
                else:
                    await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", final_total, cid)
                    await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, cid, trans_type, final_total, details_full)
                    new_balance = float(cust['balance']) + final_total
                    
                asyncio.create_task(send_push_notification(
                    customer_id=cid,
                    title=f"🔄 فاتورة مرتجع" if is_return else f"🛒 فاتورة جديدة (آجل)",
                    body=f"تم تسجيل {action_name} بقيمة {final_total} ريال. رصيد دينك الحالي: {new_balance} ريال."
                ))
            else:
                details_full = f"{action_name} كاشير (نقدي)\n{details}\nالإجمالي النهائي: {final_total}"
                await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, NULL, $2, $3, $4)", store_id, trans_type, final_total if not is_return else -final_total, details_full)

            # 5. نظام كشف التلاعب (Fraud Detection)
            if is_return:
                returns_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM transactions 
                    WHERE store_id = $1 AND trans_type = 'pos_return' 
                    AND created_at >= NOW() - INTERVAL '1 day'
                """, store_id)
                
                if returns_count >= 3:
                    # التحقق من وجود جدول fraud_logs أولاً لتجنب الأخطاء
                    try:
                        worker_name = await conn.fetchval("SELECT name FROM workers WHERE id = $1", worker_id) if worker_id else "مجهول"
                        await conn.execute("""
                            INSERT INTO fraud_logs (store_id, worker_name, action) 
                            VALUES ($1, $2, 'تم رصد 3 عمليات مرتجع/إلغاء خلال 24 ساعة!')
                        """, store_id, worker_name)
                    except: pass
                    
                    store_tid_real = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
                    if store_tid_real:
                        try: await bot.send_message(store_tid_real, "🚨 **تنبيه أمني خطير!** 🚨\n\nتم رصد 3 عمليات (مرتجع/إلغاء فاتورة) في بقالتك خلال 24 ساعة.\nيرجى مراجعة الكاميرات أو الصندوق فوراً للتأكد من عدم وجود تلاعب أو سرقة!")
                        except: pass
                        
                    try: await bot.send_message(ADMIN_ID, f"⚠️ **نظام كشف التلاعب:**\nنشاط مشبوه في بقالة رقم {store_id} (كثرة المرتجعات/الإلغاءات).")
                    except: pass
                    
        return web.json_response({"status": "success", "message": f"تم تسجيل الـ {action_name} بنجاح!"})

    except Exception as e:
        logging.error(f"POS API Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ أثناء معالجة الطلب."})

async def api_pos_close_shift(request: web.Request):
    data = await request.json()
    
    # 🛡️ حماية من البيانات الناقصة
    store_tid = data.get('store_id')
    if not store_tid:
        return web.json_response({"status": "error", "message": "معرف البقالة مفقود"})
        
    store_tid = int(store_tid)
    
    async with database.pool.acquire() as conn:
        store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", store_tid)
        if not store_id:
            store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", store_tid)
        worker_name = await conn.fetchval("SELECT name FROM workers WHERE telegram_id = $1", store_tid) or "صاحب البقالة"
        
    from services.accounting import generate_and_send_z_report
    await generate_and_send_z_report(store_id, worker_name, is_auto=False)
    return web.json_response({"status": "success"})

# =====================================================================
# 🌉 الجسر السري: سحب وإرسال الكروت إلكترونياً للزبائن بصمت
# =====================================================================
import aiohttp
import os
import logging
from aiohttp import web
import asyncio
from datetime import datetime
import database
from services.notifications import send_push_notification

ALSHEHAB_API_URL = os.getenv("ALSHEHAB_API_URL", "https://my-bot-ehio.onrender.com" )
B2B_SECRET_KEY = os.getenv("B2B_SECRET_KEY", "SuperSecretDukkaniBridge2026")

# 👇 الدالة الجديدة: جلب قائمة الفئات المتوفرة من الشهاب برو 👇
async def api_pos_get_ecards_list(request: web.Request):
    store_tid = int(request.query.get('store_id', 0))
    async with database.pool.acquire() as conn:
        from api.routes.manage import get_real_store_id
        store_id = await get_real_store_id(conn, request, store_tid)
        if not store_id:
            return web.json_response({"status": "error", "message": "البقالة غير موجودة"})
            
        alshehab_phone = await conn.fetchval("SELECT alshehab_phone FROM stores WHERE id = $1", store_id)
        if not alshehab_phone:
            return web.json_response({"status": "error", "message": "يجب ربط حساب الشهاب أولاً من الإعدادات"})

    try:
        async with aiohttp.ClientSession( ) as session:
            headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
            async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/available_cards", headers=headers, json={"store_telegram_id": alshehab_phone}) as resp:
                result = await resp.json()
                return web.json_response(result)
    except Exception as e:
        logging.error(f"POS Get Cards Error: {e}")
        return web.json_response({"status": "error", "message": "فشل الاتصال بسيرفر الشهاب برو"})

async def api_pos_pull_and_send_card(request: web.Request):
    """يسحب الكرت من الشهاب، يقيده كدين في دكاني، ويرسله لصندوق الزبون"""
    try:
        data = await request.json()
        store_tid = int(data.get('store_id', 0))
        customer_id = int(data.get('customer_id', 0))
        category = data.get('category')
        amount = float(data.get('amount', 0))
        
        if not customer_id or not category or not amount:
            return web.json_response({"status": "error", "message": "بيانات غير مكتملة"})

        async with database.pool.acquire() as conn:
            from api.routes.manage import get_real_store_id
            store_id = await get_real_store_id(conn, request, store_tid)
            if not store_id: return web.json_response({"status": "error", "message": "البقالة غير موجودة"})

            alshehab_phone = await conn.fetchval("SELECT alshehab_phone FROM stores WHERE id = $1", store_id)
            if not alshehab_phone: return web.json_response({"status": "error", "message": "يجب ربط حساب الشهاب أولاً من الإعدادات"})

            cust = await conn.fetchrow("SELECT name, balance, credit_limit FROM customers WHERE id = $1", customer_id)
            if not cust: return web.json_response({"status": "error", "message": "الزبون غير موجود"})

        # 🚨 الحماية الاستباقية: نتحقق من السقف بشكل تقريبي قبل سحب الكرت لمنع ضياعه 🚨
        if (float(cust['balance']) + amount) > float(cust['credit_limit']):
            return web.json_response({"status": "error", "message": f"لا يمكن السحب! يتجاوز سقف دين الزبون ({cust['name']})."})

        # سحب الكرت من الشهاب
        async with aiohttp.ClientSession( ) as session:
            headers = {"Authorization": f"Bearer {B2B_SECRET_KEY}", "Content-Type": "application/json"}
            payload = {"store_telegram_id": alshehab_phone, "category": category, "amount": int(amount)}
            
            async with session.post(f"{ALSHEHAB_API_URL}/api/b2b/pull_card", headers=headers, json=payload) as resp:
                result = await resp.json()

        if result.get("status") != "success":
            return web.json_response({"status": "error", "message": result.get("message", "فشل سحب الكرت من الشهاب")})

        card_data = result['card']
        sell_price = float(card_data['sell_price'])
        
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", sell_price, customer_id)
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                    VALUES ($1, $2, 'شراء_كرت', $3, $4)
                """, store_id, customer_id, sell_price, f"شراء كرت {category} فئة {amount}")
                
                # إرسال الكرت كـ (إشعار مقروء) في صندوق الوارد
                card_details = f"الشبكة: {category}\nالفئة: {amount}\nرقم التعبئة (الرمز السري):\n{card_data['pin_code']}"
                await conn.execute("""
                    INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                    VALUES ($1, $2, $3, $4)
                """, store_id, customer_id, f"💳 كرت {category} فئة {amount}", card_details)

        asyncio.create_task(send_push_notification(
            customer_id=customer_id,
            title=f"💳 تم إرسال كرت {category} لك",
            body=f"تم إرسال الكرت بنجاح إلى صندوق الوارد، وتم قيد {sell_price} ريال على حسابك.",
            extra_data={"action": "open_inbox"}
        ))
        
        return web.json_response({"status": "success", "message": "تم سحب الكرت وإرساله للزبون بنجاح!"})
        
    except Exception as e:
        logging.error(f"POS Pull Card Error: {e}")
        return web.json_response({"status": "error", "message": "حدث خطأ داخلي أثناء معالجة الكرت"})

def setup_pos_routes(app: web.Application):
    app.router.add_get('/api/pos/data', api_pos_data)
    app.router.add_post('/api/pos/order', api_pos_order)
    app.router.add_post('/api/pos/close_shift', api_pos_close_shift)
    
    # 🌉 مسارات إرسال الكروت السرية من الكاشير
    app.router.add_get('/api/pos/available_ecards', api_pos_get_ecards_list)
    app.router.add_post('/api/pos/pull_and_send_card', api_pos_pull_and_send_card)
