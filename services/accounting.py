# مسار الملف: services/accounting.py
import logging
import asyncio
import database
from api.routes.websockets import notify_store_new_order
from bot.setup import bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

async def process_new_order(store_id: int, customer_id: int, items: dict, payment_method: str, delivery_fee: float = 0.0):
    """
    المطبخ المركزي لمعالجة الطلبات الجديدة (مضاد للثغرات)
    """
    # تجهيز المتغيرات لاستخدامها خارج الـ Transaction
    order_id = None
    grand_total = 0.0
    cust_name = "زبون نقدي"
    store_tid = None
    enriched_items = {} # لتخزين الأسعار والأسماء ومنع الاستعلامات المكررة

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            
            # 1. حساب الإجمالي الفعلي والتحقق من الكميات
            total_items_price = 0.0
            for pid_str, item in items.items():
                pid = int(pid_str)
                qty = int(item.get('qty', 1))
                
                # 🛡️ سد ثغرة الكميات السالبة
                if qty <= 0:
                    raise ValueError("الكمية يجب أن تكون أكبر من الصفر.")
                    
                # جلب السعر والاسم مرة واحدة فقط
                row = await conn.fetchrow("SELECT price, custom_name FROM store_products WHERE id = $1 AND store_id = $2 AND is_deleted = FALSE", pid, store_id)
                if not row:
                    raise ValueError(f"عذراً، أحد المنتجات في سلتك لم يعد متوفراً.")
                
                db_price = float(row['price'])
                total_items_price += (db_price * qty)
                
                # حفظ البيانات لاستخدامها لاحقاً بدون استعلام جديد
                enriched_items[pid] = {
                    'qty': qty,
                    'price': db_price,
                    'name': row['custom_name'] or f"منتج #{pid}"
                }
            
            grand_total = total_items_price + delivery_fee

            # 2. فحص سقف الدين وحالة الزبون (مع قفل الصفوف FOR UPDATE)
            if payment_method == 'credit' and customer_id != 0:
                # 🛡️ استخدام FOR UPDATE لمنع ثغرة تجاوز الدين (Race Condition)
                cust = await conn.fetchrow("SELECT name, balance, credit_limit, status, is_deleted FROM customers WHERE id = $1 FOR UPDATE", customer_id)
                
                if not cust or cust['is_deleted']:
                    raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                if cust['status'] == 'suspended':
                    raise ValueError("حسابك موقوف مؤقتاً، لا يمكنك الطلب بالآجل.")
                    
                cust_name = cust['name']
                if float(cust['balance']) + grand_total > float(cust['credit_limit']):
                    raise ValueError(f"الطلب يتجاوز سقف الدين المسموح لك. السقف: {cust['credit_limit']} ريال.")
            elif customer_id != 0:
                cust_name = await conn.fetchval("SELECT name FROM customers WHERE id = $1", customer_id) or "زبون نقدي"

            # 3. إنشاء الطلب
            order_id = await conn.fetchval("""
                INSERT INTO orders (store_id, customer_id, status, payment_method, total_amount, delivery_fee)
                VALUES ($1, $2, 'pending', $3, $4, $5) RETURNING id
            """, store_id, customer_id if customer_id != 0 else None, payment_method, grand_total, delivery_fee)

            # 4. إدراج تفاصيل الطلب وخصم المخزون بأمان
            for pid, item_data in enriched_items.items():
                qty = item_data['qty']
                db_price = item_data['price']
                
                await conn.execute("""
                    INSERT INTO order_items (order_id, product_id, quantity, price_at_time)
                    VALUES ($1, $2, $3, $4)
                """, order_id, pid, qty, db_price)
                
                # 🛡️ سد ثغرة المخزون السالب
                result = await conn.execute("""
                    UPDATE store_products 
                    SET stock = stock - $1 
                    WHERE id = $2 AND (stock IS NULL OR stock >= $1)
                """, qty, pid)
                
                if result == "UPDATE 0":
                    raise ValueError(f"عذراً، الكمية المطلوبة من {item_data['name']} غير متوفرة في المخزون.")

            # 5. تسجيل المعاملة المالية
            if payment_method == 'credit' and customer_id != 0:
                await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", grand_total, customer_id)
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($1, $2, $3, 'credit_sale', $4, $5)
                """, store_id, customer_id, order_id, grand_total, f'مبيعات آجلة (طلب #{order_id})')
            else:
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($1, $2, $3, 'cash_sale', $4, $5)
                """, store_id, customer_id if customer_id != 0 else None, order_id, grand_total, f'مبيعات نقدية (طلب #{order_id})')

            # 6. إنشاء الفاتورة اللحظية
            if customer_id != 0:
                invoice_title = f"فاتورة طلب رقم #{order_id}"
                invoice_url = f"/api/customer/invoice/{order_id}"
                await conn.execute("""
                    INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                    VALUES ($1, $2, $3, $4)
                """, store_id, customer_id, invoice_title, invoice_url)

            # جلب معرف تيليجرام للبقالة لاستخدامه خارج الـ Transaction
            store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
            
    # 👇 إرجاع رقم الطلب والإجمالي لكي تستقبله شاشة الزبون بدون أخطاء 👇
    return order_id, grand_total

async def process_pos_transaction(store_id: int, worker_id: int, customer_id: int, total: float, discount: float, is_return: bool, items: dict):
    """
    المطبخ المركزي لمعالجة طلبات ومرتجعات الكاشير السريع (مضاد للثغرات)
    """
    if total < 0:
        raise ValueError("إجمالي الفاتورة لا يمكن أن يكون بالسالب.")
        
    new_balance = 0.0
    cust_name = "زبون نقدي"
    fraud_alert_triggered = False
    store_tid = None

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. فحص سقف الدين وحالة الزبون (مع قفل الصفوف FOR UPDATE)
            if customer_id != 0:
                cust = await conn.fetchrow("SELECT name, balance, credit_limit, status, is_deleted FROM customers WHERE id = $1 FOR UPDATE", customer_id)
                if not cust or cust['is_deleted']:
                    raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                if cust['status'] == 'suspended':
                    raise ValueError("حساب الزبون موقوف مؤقتاً.")
                
                cust_name = cust['name']
                if not is_return and (float(cust['balance']) + total > float(cust['credit_limit'])):
                    raise ValueError(f"الطلب يتجاوز سقف الدين المسموح للزبون ({cust_name}).")

            # 2. تحديث المخزون بأمان (ومنع الكميات السالبة)
            for pid_str, item in items.items():
                pid = int(pid_str)
                qty = int(item.get('qty', 1))
                if qty <= 0:
                    raise ValueError("الكمية يجب أن تكون أكبر من الصفر.")
                    
                if is_return:
                    await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", qty, pid)
                else:
                    result = await conn.execute("UPDATE store_products SET stock = stock - $1 WHERE id = $2 AND (stock IS NULL OR stock >= $1)", qty, pid)
                    if result == "UPDATE 0":
                        p_name = item.get('name', f'منتج #{pid}')
                        raise ValueError(f"الكمية المطلوبة من {p_name} غير متوفرة في المخزون.")

            # 3. تجهيز التفاصيل وتحديث الرصيد
            details = "\n".join([f"- {item['qty']}x {item['name']}" for item in items.values()])
            if discount > 0:
                details += f"\n🎁 الخصم: {discount} ريال"
                
            trans_type = 'pos_return' if is_return else 'pos_order'
            action_name = "مرتجع" if is_return else "طلب"
            
            if customer_id != 0:
                details_full = f"{action_name} كاشير (آجل)\n{details}\nالإجمالي النهائي: {total}"
                if is_return:
                    await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", total, customer_id)
                    await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, customer_id, trans_type, -total, details_full)
                    new_balance = float(cust['balance']) - total
                else:
                    await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", total, customer_id)
                    await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, $2, $3, $4, $5)", store_id, customer_id, trans_type, total, details_full)
                    new_balance = float(cust['balance']) + total
            else:
                details_full = f"{action_name} كاشير (نقدي)\n{details}\nالإجمالي النهائي: {total}"
                await conn.execute("INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) VALUES ($1, NULL, $2, $3, $4)", store_id, trans_type, total if not is_return else -total, details_full)

            # 4. نظام كشف التلاعب (Fraud Detection)
            if is_return:
                returns_count = await conn.fetchval("""
                    SELECT COUNT(*) FROM transactions 
                    WHERE store_id = $1 AND trans_type = 'pos_return' 
                    AND created_at >= NOW() - INTERVAL '1 day'
                """, store_id)
                
                if returns_count >= 3:
                    worker_name = "مجهول"
                    if worker_id:
                        worker_name = await conn.fetchval("SELECT name FROM workers WHERE id = $1", worker_id) or "مجهول"
                        
                    await conn.execute("""
                        INSERT INTO fraud_logs (store_id, worker_name, action) 
                        VALUES ($1, $2, 'تم رصد 3 عمليات مرتجع/إلغاء خلال 24 ساعة!')
                    """, store_id, worker_name)
                    
                    fraud_alert_triggered = True
                    store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)

    # ==========================================
    # 🚀 خارج الـ Transaction: إرسال الإشعارات
    # ==========================================
    if customer_id != 0:
        from services.notifications import send_push_notification
        action_name = "مرتجع" if is_return else "طلب"
        asyncio.create_task(send_push_notification(
            customer_id,
            f"🔄 فاتورة مرتجع" if is_return else f"🛒 فاتورة جديدة (آجل)",
            f"تم تسجيل {action_name} بقيمة {total} ريال. رصيد دينك الحالي: {new_balance} ريال."
        ))

    if fraud_alert_triggered:
        if store_tid:
            try:
                await bot.send_message(store_tid, "🚨 **تنبيه أمني خطير!** 🚨\n\nتم رصد 3 عمليات (مرتجع/إلغاء فاتورة) في بقالتك خلال 24 ساعة.\nيرجى مراجعة الكاميرات أو الصندوق فوراً للتأكد من عدم وجود تلاعب أو سرقة!")
            except: pass
        try:
            from config import ADMIN_ID
            await bot.send_message(ADMIN_ID, f"⚠️ **نظام كشف التلاعب:**\nنشاط مشبوه في بقالة رقم {store_id} (كثرة المرتجعات/الإلغاءات).")
        except: pass

    return new_balance, cust_name

async def process_payment(store_id: int, customer_id: int, amount: float):
    """
    المطبخ المركزي لمعالجة سداد الديون وتوليد سندات القبض
    """
    if amount <= 0:
        raise ValueError("مبلغ السداد يجب أن يكون أكبر من الصفر.")

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. قفل صف الزبون لمنع التضارب
            cust = await conn.fetchrow("SELECT name, balance, is_deleted FROM customers WHERE id = $1 AND store_id = $2 FOR UPDATE", customer_id, store_id)
            
            if not cust or cust['is_deleted']:
                raise ValueError("حساب الزبون غير موجود أو تم حذفه.")
                
            # 2. خصم المبلغ وتسجيل العملية
            await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", amount, customer_id)
            trans_id = await conn.fetchval("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'payment', $3, 'سداد دفعة نقدية') RETURNING id
            """, store_id, customer_id, -amount)
            
            new_balance = float(cust['balance']) - amount
            
            # 3. توليد سند قبض وإرساله لصندوق الوارد
            receipt_title = f"💵 سند قبض: {amount} ريال"
            receipt_url = f"/api/customer/receipt/{trans_id}"
            await conn.execute("""
                INSERT INTO invoices (store_id, customer_id, title, pdf_url)
                VALUES ($1, $2, $3, $4)
            """, store_id, customer_id, receipt_title, receipt_url)

    # ==========================================
    # 🚀 خارج الـ Transaction: إرسال الإشعارات
    # ==========================================
    from services.notifications import send_push_notification
    asyncio.create_task(send_push_notification(
        customer_id, 
        "💰 تم استلام دفعتك", 
        f"تم خصم {amount} ريال من حسابك. المتبقي عليك: {new_balance} ريال. شكراً لك!",
        {"action": "open_inbox"}
    ))
    
    return new_balance


async def process_balance_transfer(store_id: int, from_id: int, to_id: int, amount: float):
    """
    المطبخ المركزي لمعالجة تحويل الرصيد بين الزبائن بأمان تام
    """
    if amount <= 0:
        raise ValueError("مبلغ التحويل يجب أن يكون أكبر من الصفر.")
    if from_id == to_id:
        raise ValueError("لا يمكن التحويل لنفس الزبون.")

    # ترتيب الـ IDs لمنع الـ Deadlocks عند قفل الصفوف
    first_id, second_id = (from_id, to_id) if from_id < to_id else (to_id, from_id)

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. قفل الصفين بترتيب ثابت لمنع التعليق المتبادل (Deadlock)
            await conn.execute("SELECT id FROM customers WHERE id = $1 FOR UPDATE", first_id)
            await conn.execute("SELECT id FROM customers WHERE id = $1 FOR UPDATE", second_id)
            
            # 2. جلب بيانات الزبائن
            from_cust = await conn.fetchrow("SELECT name FROM customers WHERE id = $1 AND store_id = $2 AND is_deleted = FALSE", from_id, store_id)
            to_cust = await conn.fetchrow("SELECT name FROM customers WHERE id = $1 AND store_id = $2 AND is_deleted = FALSE", to_id, store_id)
            
            if not from_cust or not to_cust:
                raise ValueError("أحد الزبائن غير موجود أو تم حذفه.")

            # 3. الخصم من الأول (الذي دفع/نقص دينه)
            await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", amount, from_id)
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'transfer_out', $3, $4)
            """, store_id, from_id, -amount, f"تحويل رصيد إلى الزبون: {to_cust['name']}")
            
            # 4. الإضافة للثاني (الذي زاد دينه)
            await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", amount, to_id)
            await conn.execute("""
                INSERT INTO transactions (store_id, customer_id, trans_type, amount, details) 
                VALUES ($1, $2, 'transfer_in', $3, $4)
            """, store_id, to_id, amount, f"استلام رصيد محول من الزبون: {from_cust['name']}")

    # ==========================================
    # 🚀 خارج الـ Transaction: إرسال الإشعارات
    # ==========================================
    from services.notifications import send_push_notification
    asyncio.create_task(send_push_notification(
        from_id, 
        "💸 تم تحويل الرصيد", 
        f"تم خصم {amount} ريال من حسابك وتحويلها إلى {to_cust['name']}."
    ))
    asyncio.create_task(send_push_notification(
        to_id, 
        "💸 رصيد مضاف", 
        f"تم إضافة {amount} ريال إلى حسابك محولة من {from_cust['name']}."
    ))

async def process_order_rejection(store_id: int, order_id: int, worker_id: int = None):
    """
    المطبخ المركزي لمعالجة رفض الطلبات واسترجاع الأموال والمخزون
    """
    customer_id = None
    total_amount = 0.0

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. جلب بيانات الطلب وقفل الصف
            order = await conn.fetchrow("SELECT customer_id, total_amount, payment_method, status FROM orders WHERE id = $1 AND store_id = $2 FOR UPDATE", order_id, store_id)
            
            if not order:
                raise ValueError("الطلب غير موجود.")
            if order['status'] != 'pending':
                raise ValueError("لا يمكن رفض طلب قيد المعالجة أو مكتمل.")
                
            customer_id = order['customer_id']
            total_amount = float(order['total_amount'])
            
            # 2. تحديث حالة الطلب
            await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
            
            # 3. استرجاع المخزون
            items = await conn.fetch("SELECT product_id, quantity FROM order_items WHERE order_id = $1", order_id)
            for item in items:
                await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", item['quantity'], item['product_id'])
            
            # 4. استرجاع الرصيد (إذا كان الطلب آجلاً)
            if order['payment_method'] == 'credit' and customer_id:
                # قفل صف الزبون
                await conn.execute("SELECT id FROM customers WHERE id = $1 FOR UPDATE", customer_id)
                await conn.execute("UPDATE customers SET balance = balance - $1 WHERE id = $2", total_amount, customer_id)
                await conn.execute("""
                    INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                    VALUES ($1, $2, $3, 'refund', $4, 'إلغاء طلب واسترجاع الرصيد')
                """, store_id, customer_id, order_id, -total_amount)
                
            # 5. تسجيل التلاعب الأمني
            worker_name = "مستخدم النظام"
            if worker_id:
                worker_name = await conn.fetchval("SELECT name FROM workers WHERE id = $1", worker_id) or "مستخدم النظام"
                
            await conn.execute("""
                INSERT INTO fraud_logs (store_id, worker_name, action) 
                VALUES ($1, $2, 'قام برفض وإلغاء طلب رقم ' || $3)
            """, store_id, worker_name, str(order_id))

    # ==========================================
    # 🚀 خارج الـ Transaction: إرسال الإشعارات
    # ==========================================
    if customer_id:
        from services.notifications import send_push_notification
        asyncio.create_task(send_push_notification(
            customer_id, 
            "❌ نعتذر منك", 
            "تم رفض طلبك من قبل البقالة. تم استرجاع رصيدك."
        ))


async def process_family_order_action(parent_id: int, order_id: int, action: str, modified_items: dict):
    """
    المطبخ المركزي لمعالجة طلبات العائلة (اعتماد أو رفض)
    """
    child_id = None
    store_id = None
    new_total = 0.0
    final_items_for_store = {}
    father_name = ""

    async with database.pool.acquire() as conn:
        async with conn.transaction():
            # 1. جلب الطلب وقفل الصف
            order = await conn.fetchrow("""
                SELECT o.id, o.store_id, o.total_amount, c.name, c.id as child_id
                FROM orders o JOIN customers c ON o.customer_id = c.id
                WHERE o.id = $1 AND c.parent_id = $2 AND o.status = 'family_review' FOR UPDATE
            """, order_id, parent_id)
            
            if not order:
                raise ValueError("الطلب غير موجود أو تمت معالجته مسبقاً.")
                
            child_id = order['child_id']
            store_id = order['store_id']
            
            if action == 'approve':
                # قفل صف الأب لتحديث الرصيد
                father = await conn.fetchrow("SELECT name, balance, credit_limit FROM customers WHERE id = $1 FOR UPDATE", parent_id)
                father_name = father['name']
                
                original_items = await conn.fetch("SELECT product_id, quantity, price_at_time FROM order_items WHERE order_id = $1", order_id)
                
                for orig_item in original_items:
                    pid = orig_item['product_id']
                    pid_str = str(pid)
                    old_qty = orig_item['quantity']
                    price = float(orig_item['price_at_time'])
                    
                    new_qty = int(modified_items.get(pid_str, old_qty))
                    if new_qty < 0: new_qty = 0
                    
                    if new_qty != old_qty:
                        diff = old_qty - new_qty
                        # 🛡️ حماية: التأكد من أن المخزون لا يصبح بالسالب عند زيادة الكمية (diff بالسالب)
                        res = await conn.execute("""
                            UPDATE store_products 
                            SET stock = stock + $1 
                            WHERE id = $2 AND (stock IS NULL OR stock + $1 >= 0)
                        """, diff, pid)
                        
                        if res == "UPDATE 0":
                            p_name = await conn.fetchval("SELECT custom_name FROM store_products WHERE id = $1", pid)
                            raise ValueError(f"عذراً، الكمية الإضافية المطلوبة من ({p_name}) غير متوفرة في المخزون.")
                        
                        if new_qty == 0:
                            await conn.execute("DELETE FROM order_items WHERE order_id = $1 AND product_id = $2", order_id, pid)
                        else:
                            await conn.execute("UPDATE order_items SET quantity = $1 WHERE order_id = $2 AND product_id = $3", new_qty, order_id, pid)
                    
                    if new_qty > 0:
                        new_total += price * new_qty
                        p_name = await conn.fetchval("SELECT custom_name FROM store_products WHERE id = $1", pid)
                        final_items_for_store[pid_str] = {"qty": new_qty, "name": p_name}
                
                if new_total == 0:
                    await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
                    action = 'rejected_empty'
                else:
                    # التحقق من سقف الدين للأب
                    if float(father['balance']) + new_total > float(father['credit_limit']):
                        raise ValueError("الطلب يتجاوز سقف الدين المسموح لك.")
                        
                    await conn.execute("UPDATE orders SET status = 'pending', total_amount = $1, customer_id = $2 WHERE id = $3", new_total, parent_id, order_id)
                    await conn.execute("UPDATE customers SET balance = balance + $1 WHERE id = $2", new_total, parent_id)
                    await conn.execute("""
                        INSERT INTO transactions (store_id, customer_id, order_id, trans_type, amount, details)
                        VALUES ($1, $2, $3, 'credit_sale', $4, $5)
                    """, store_id, parent_id, order_id, new_total, f"طلب عائلي معتمد")
                    
            else:
                # حالة الرفض
                await conn.execute("UPDATE orders SET status = 'rejected' WHERE id = $1", order_id)
                items = await conn.fetch("SELECT product_id, quantity FROM order_items WHERE order_id = $1", order_id)
                for item in items:
                    await conn.execute("UPDATE store_products SET stock = stock + $1 WHERE id = $2 AND stock IS NOT NULL", item['quantity'], item['product_id'])

    # ==========================================
    # 🚀 خارج الـ Transaction: إرسال الإشعارات
    # ==========================================
    from services.notifications import send_push_notification
    
    if action == 'approve':
        asyncio.create_task(send_push_notification(child_id, "✅ تم الاعتماد", "تم اعتماد طلبك وإرساله للبقالة."))
        
        # إرسال الطلب للبقالة
        order_data = {
            "order_id": order_id, "customer_name": father_name,
            "total_amount": new_total, "payment_method": "credit", "items": final_items_for_store
        }
        asyncio.create_task(notify_store_new_order(store_id, order_data))
        
        # إرسال رسالة تيليجرام للبقالة
        async with database.pool.acquire() as conn:
            store_tid = await conn.fetchval("SELECT telegram_id FROM stores WHERE id = $1", store_id)
        if store_tid:
            items_text = "\n".join([f"- {item['qty']}x {item['name']}" for item in final_items_for_store.values()])
            msg_text = f"🔔 **طلب جديد رقم #{order_id}**\n👤 الزبون: {father_name}\n💰 الإجمالي: {new_total} ريال\n💳 الدفع: آجل (دين)\n\n**التفاصيل:**\n{items_text}"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ قبول وتجهيز", callback_data=f"accept_order_{order_id}")],
                [InlineKeyboardButton(text="❌ رفض الطلب", callback_data=f"reject_order_{order_id}")]
            ])
            try:
                await bot.send_message(store_tid, msg_text, reply_markup=kb)
            except: pass
            
    elif action == 'rejected_empty':
        asyncio.create_task(send_push_notification(child_id, "❌ تم الإلغاء", "تم إلغاء الطلب بالكامل لعدم وجود منتجات."))
    else:
        asyncio.create_task(send_push_notification(child_id, "❌ تم الرفض", "تم رفض طلبك من قبل رب الأسرة."))

async def generate_and_send_z_report(store_id: int, worker_name: str, is_auto: bool = False):
    """توليد تقرير الوردية وإرساله للأرشيف أو صاحب البقالة"""
    async with database.pool.acquire() as conn:
        store = await conn.fetchrow("SELECT name, telegram_id, archive_group_id FROM stores WHERE id = $1", store_id)
        if not store: return

        sales = await conn.fetchrow("""
            SELECT 
                COALESCE(SUM(CASE WHEN trans_type = 'pos_order' THEN amount ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN trans_type = 'pos_return' THEN amount ELSE 0 END), 0) as total_returns,
                COALESCE(SUM(CASE WHEN trans_type = 'payment' THEN ABS(amount) ELSE 0 END), 0) as total_payments
            FROM transactions 
            WHERE store_id = $1 AND DATE(created_at) = CURRENT_DATE
        """, store_id)

        if is_auto and sales['total_sales'] == 0 and sales['total_payments'] == 0 and sales['total_returns'] == 0:
            return

        from datetime import datetime
        msg = f"📊 **تقرير نهاية الوردية (Z-Report)**\n🏪 البقالة: {store['name']}\n"
        msg += f"👷‍♂️ الموظف: {worker_name}\n"
        msg += f"📅 التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        msg += f"🛒 المبيعات: {sales['total_sales']} ريال\n"
        msg += f"🔄 المرتجعات: {abs(sales['total_returns'])} ريال\n"
        msg += f"💰 السدادات المستلمة: {sales['total_payments']} ريال\n"
        
        if is_auto:
            msg += "\n⚠️ *ملاحظة: العامل لم يقم بتقفيل الوردية، تم التقفيل وإرسال التقرير تلقائياً بواسطة النظام لحفظ الحسابات.*"

        target_chat = store['archive_group_id'] if store['archive_group_id'] else store['telegram_id']
        
        if target_chat:
            from bot.setup import bot
            try:
                await bot.send_message(target_chat, msg, parse_mode="Markdown")
            except Exception: pass
