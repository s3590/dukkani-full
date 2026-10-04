# مسار الملف: database.py
import asyncio
import asyncpg
import logging
import csv
from io import StringIO
from datetime import datetime
from config import DATABASE_URL

pool = None

async def init_db():
    global pool
    if pool is not None:
        return 
        
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL, 
            min_size=1,
            max_size=20, # زيادة الحد الأقصى للاتصالات لتحمل الضغط
            server_settings={'timezone': 'Asia/Aden'}
        )
        
        async with pool.acquire() as conn:
            schema_query = """          
            -- 1. جدول البقالات
            CREATE TABLE IF NOT EXISTS stores (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE, 
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20) UNIQUE,
                status VARCHAR(20) DEFAULT 'active', 
                package_type VARCHAR(20) DEFAULT 'free',
                subscription_end TIMESTAMP,
                trial_ends_at TIMESTAMP, -- ⏱️ نهاية الفترة التجريبية
                suspend_reason TEXT, -- 🔴 سبب الإيقاف (الذي برمجناه في لوحة التحكم)
                is_deleted BOOLEAN DEFAULT FALSE, -- 🗑️ سلة المهملات
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 2. جدول الزبائن
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                parent_id INT REFERENCES customers(id) ON DELETE SET NULL,
                phone VARCHAR(20) NOT NULL,
                name VARCHAR(100) NOT NULL,
                password VARCHAR(255) NOT NULL,
                balance NUMERIC DEFAULT 0.0,
                credit_limit NUMERIC DEFAULT 50000,
                address TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                is_deleted BOOLEAN DEFAULT FALSE, -- 🗑️ سلة المهملات
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(store_id, phone)
            );

            -- 3. جدول الكتالوج المركزي
            CREATE TABLE IF NOT EXISTS global_products (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                category VARCHAR(50) NOT NULL,
                image_url TEXT DEFAULT '/static/logo_customer.png'
            );

            -- 4. جدول منتجات البقالات
            CREATE TABLE IF NOT EXISTS store_products (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                product_id INT REFERENCES global_products(id) ON DELETE CASCADE,
                custom_name VARCHAR(100), 
                category VARCHAR(50) DEFAULT 'عام',
                cost_price NUMERIC DEFAULT 0.0,
                price NUMERIC NOT NULL,
                telegram_file_id TEXT,
                stock INT DEFAULT NULL, 
                low_stock_alert INT DEFAULT 5,
                is_available BOOLEAN DEFAULT TRUE,
                barcode VARCHAR(50),
                expires_at TIMESTAMP DEFAULT NULL,
                is_deleted BOOLEAN DEFAULT FALSE -- 🗑️ سلة المهملات
            );

            -- 5. جدول المناديب (عمال التوصيل)
            CREATE TABLE IF NOT EXISTS delivery_agents (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20),
                is_active BOOLEAN DEFAULT TRUE,
                is_deleted BOOLEAN DEFAULT FALSE -- 🗑️ سلة المهملات
            );

            -- 6. جدول الطلبات
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                agent_id INT REFERENCES delivery_agents(id) ON DELETE SET NULL,
                status VARCHAR(20) DEFAULT 'pending',
                payment_method VARCHAR(20) DEFAULT 'credit',
                total_amount NUMERIC NOT NULL,
                delivery_fee NUMERIC DEFAULT 0.0,
                delivery_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 7. جدول تفاصيل الطلب
            CREATE TABLE IF NOT EXISTS order_items (
                id SERIAL PRIMARY KEY,
                order_id INT REFERENCES orders(id) ON DELETE CASCADE,
                product_id INT REFERENCES store_products(id) ON DELETE CASCADE,
                quantity INT NOT NULL,
                price_at_time NUMERIC NOT NULL
            );

            -- 8. جدول المعاملات المالية
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                order_id INT REFERENCES orders(id) ON DELETE SET NULL,
                trans_type VARCHAR(50) NOT NULL,
                amount NUMERIC NOT NULL, 
                details TEXT, 
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 9. جدول العمال والصلاحيات
            CREATE TABLE IF NOT EXISTS workers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                telegram_id BIGINT UNIQUE,
                name VARCHAR(100) NOT NULL,
                passcode VARCHAR(20) UNIQUE NOT NULL,
                permissions TEXT DEFAULT '["pos"]',
                is_deleted BOOLEAN DEFAULT FALSE, -- 🗑️ سلة المهملات
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 10. جدول اشتراكات الإشعارات
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                user_id INT PRIMARY KEY,
                user_type VARCHAR(20),
                subscription_json TEXT
            );

            -- 11. جدول سجل العمليات (Terminal Logs) 🚀 الجديد
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                action VARCHAR(255) NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 12. جدول تذاكر الدعم الفني 🎧 الجديد
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                subject VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                status VARCHAR(20) DEFAULT 'open', -- open, answered, closed
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 13. جدول الفواتير وصندوق الوارد 📥 الجديد
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                title VARCHAR(255) NOT NULL,
                pdf_url TEXT NOT NULL,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 14. جدول إعدادات النظام (وضع الصيانة) 🛠️
            CREATE TABLE IF NOT EXISTS system_settings (
                id SERIAL PRIMARY KEY,
                maintenance_mode BOOLEAN DEFAULT FALSE
            );

            -- 15. جدول القائمة السوداء لتوكنات الظل 🛑
            CREATE TABLE IF NOT EXISTS blacklisted_tokens (
                jti VARCHAR(50) PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 16. جدول محادثات الدعم الفني 💬
            CREATE TABLE IF NOT EXISTS admin_store_chats (
                id SERIAL PRIMARY KEY,
                store_id INT,
                sender VARCHAR(20),
                message TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 17. جدول الموردين (تجار الجملة) 🏭 الجديد
            CREATE TABLE IF NOT EXISTS suppliers (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                name VARCHAR(100) NOT NULL,
                phone VARCHAR(20),
                balance NUMERIC DEFAULT 0.0, -- ديون البقالة للمورد
                is_deleted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 18. جدول المصروفات اليومية والمشتريات 💸 الجديد
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                supplier_id INT REFERENCES suppliers(id) ON DELETE SET NULL, -- يرتبط بالمورد إن وُجد
                amount NUMERIC NOT NULL,
                category VARCHAR(50) NOT NULL, -- (مشتريات بضاعة، رواتب، كهرباء، إيجار، أخرى)
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 19. جدول الجرد الذكي (التسويات) 📦 الجديد
            CREATE TABLE IF NOT EXISTS inventory_checks (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                product_id INT REFERENCES store_products(id) ON DELETE CASCADE,
                old_stock INT, -- الكمية قبل الجرد
                new_stock INT NOT NULL, -- الكمية الفعلية المجرودة
                difference INT NOT NULL, -- (عجز أو زيادة)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
            await conn.execute(schema_query)
            
            # إدراج صف الإعدادات الافتراضي إذا لم يكن موجوداً
            await conn.execute("INSERT INTO system_settings (id, maintenance_mode) VALUES (1, FALSE) ON CONFLICT (id) DO NOTHING;")

            # =================================================================
            # 👇 كود الترقيع التلقائي للجداول القديمة (تحديث شامل) 👇
            # =================================================================
            alter_queries = [
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS alshehab_phone VARCHAR(50);",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS alshehab_password VARCHAR(255);",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS telecom_permissions JSONB DEFAULT '{\"paper_cards\": false, \"ecards\": false, \"instant_recharge\": false}'::jsonb;",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS can_pull_cards BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS archive_group_id BIGINT;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS carton_qty INT DEFAULT 0;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS carton_price NUMERIC DEFAULT 0.0;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS carton_barcode VARCHAR(50);",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS is_fresh_item BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS unit_name VARCHAR(20) DEFAULT 'حبة';", # 🆕 وحدة المفرد
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS bulk_name VARCHAR(20) DEFAULT 'كرتون';", # 🆕 وحدة الجملة
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active';",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS address TEXT;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP DEFAULT NULL;",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS parent_id INT REFERENCES customers(id) ON DELETE SET NULL;",
                "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS order_id INT REFERENCES orders(id) ON DELETE SET NULL;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE workers ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE delivery_agents ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMP;",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS suspend_reason TEXT;",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS theme_color VARCHAR(20) DEFAULT '#0ba360';",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS logo_url TEXT DEFAULT '/static/logo_customer.png';",
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS alshehab_phone VARCHAR(20);",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS can_pull_cards BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS offer_text TEXT;",
                "ALTER TABLE store_products ADD COLUMN IF NOT EXISTS cost_price NUMERIC DEFAULT 0.0;",
                "ALTER TABLE customers ADD COLUMN IF NOT EXISTS family_direct_order BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE delivery_agents ADD COLUMN IF NOT EXISTS passcode VARCHAR(20);",
                "ALTER TABLE admin_logs ADD COLUMN IF NOT EXISTS ip_address VARCHAR(50);" # 👈 تتبع الـ IP للمدير
            ]
            
            for query in alter_queries:
                try:
                    await conn.execute(query)
                except Exception as e:
                    logging.warning(f"ملاحظة أثناء تنفيذ الترقيع: {e}")

            # تم فصل هذا السطر وتغليفه لمنع الأخطاء إذا كان نوع الحقل متطابقاً مسبقاً
            try:
                await conn.execute("ALTER TABLE customers ALTER COLUMN phone TYPE VARCHAR(50);")
            except Exception:
                pass

            try:
                # 👇 ترقيع نظام المحادثة الفورية (In-App Chat) 👇
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id SERIAL PRIMARY KEY,
                    store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                    customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                    sender_type VARCHAR(20) NOT NULL,
                    message_text TEXT NOT NULL,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_store_cust ON chat_messages(store_id, customer_id);")

                # 👇 ترقيع نظام المحادثة العائلية (Family Chat) 👇
                await conn.execute("""
                CREATE TABLE IF NOT EXISTS family_chat_messages (
                    id SERIAL PRIMARY KEY,
                    main_customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                    sub_customer_id INT REFERENCES customers(id) ON DELETE CASCADE,
                    sender_id INT REFERENCES customers(id) ON DELETE CASCADE,
                    message_text TEXT NOT NULL,
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
            except Exception as e:
                logging.warning(f"ملاحظة أثناء إنشاء جداول المحادثات: {e}")
            
            # إنشاء فهارس (Indexes) لتسريع البحث
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_store ON orders(store_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_trans_store ON transactions(store_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_products_store ON store_products(store_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_time ON admin_logs(created_at DESC);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id);") # 🆕 فهرس صندوق الوارد
            
            # فهارس الجداول الجديدة 🚀
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_store ON expenses(store_id);")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_store ON inventory_checks(store_id);")
            
            # ==========================================
            # 🕵️‍♂️ نظام كشف التلاعب (Fraud Detection)
            # ==========================================
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS fraud_logs (
                id SERIAL PRIMARY KEY,
                store_id INT REFERENCES stores(id) ON DELETE CASCADE,
                worker_name TEXT,
                action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """)

            # ====================================================================
            # 🌟 تحديثات الربط مع نظام الشهاب برو (الخدمة الذاتية والربط) 🌟
            # ====================================================================
            
            # 1. إضافة عمود لحفظ رقم الهاتف المربوط بالشهاب برو في جدول البقالات
            await conn.execute("""
                ALTER TABLE stores 
                ADD COLUMN IF NOT EXISTS alshehab_phone VARCHAR(20) DEFAULT NULL;
            """)

            # 2. إضافة صلاحية "الخدمة الذاتية" للزبائن لسحب الكروت مباشرة
            await conn.execute("""
                ALTER TABLE customers 
                ADD COLUMN IF NOT EXISTS can_pull_cards BOOLEAN DEFAULT FALSE;
            """)
            
            # ====================================================================

            # 👇 تشغيل المنظف التلقائي في الخلفية 👇
            asyncio.create_task(run_auto_cleaner())
            asyncio.create_task(run_daily_tasks())

            logging.info("✅ تم تجهيز قاعدة البيانات الشاملة بنجاح.")
            
    except Exception as e:
        logging.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")

async def close_db():
    global pool
    if pool:
        await pool.close()
        logging.info("تم إغلاق قاعدة البيانات بأمان.")

# ==========================================
# 🧹 المنظف التلقائي والأرشفة (Auto-Cleaner & Archiver)
# ==========================================
async def run_auto_cleaner():
    """يعمل في الخلفية كل 24 ساعة لأرشفة البيانات القديمة وحذفها بأمان"""
    await asyncio.sleep(60) # الانتظار دقيقة بعد تشغيل السيرفر
    
    while True:
        try:
            if pool:
                # 🛡️ 1. جلب البيانات القديمة (نفتح الاتصال لثوانٍ معدودة ونغلقه)
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM blacklisted_tokens WHERE created_at < NOW() - INTERVAL '1 day'")
                    
                    old_logs = await conn.fetch("SELECT * FROM admin_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000")
                    old_frauds = await conn.fetch("SELECT * FROM fraud_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000")
                    old_chats = await conn.fetch("SELECT * FROM chat_messages WHERE created_at < NOW() - INTERVAL '60 days' LIMIT 5000")
                    deleted_products = await conn.fetch("SELECT * FROM store_products WHERE is_deleted = TRUE LIMIT 5000")
                    deleted_workers = await conn.fetch("SELECT * FROM workers WHERE is_deleted = TRUE LIMIT 5000")
                    
                total_records = len(old_logs) + len(old_frauds) + len(old_chats) + len(deleted_products) + len(deleted_workers)
                
                # 🛡️ 2. الأرشفة في تيليجرام (عملية بطيئة تتم خارج اتصال قاعدة البيانات)
                if total_records > 0:
                    try:
                        csv_file = StringIO()
                        writer = csv.writer(csv_file)
                        writer.writerow(["الجدول", "البيانات"])
                        
                        for row in old_logs: writer.writerow(["admin_logs", str(dict(row))])
                        for row in old_frauds: writer.writerow(["fraud_logs", str(dict(row))])
                        for row in old_chats: writer.writerow(["chat_messages", str(dict(row))])
                        for row in deleted_products: writer.writerow(["store_products", str(dict(row))])
                        for row in deleted_workers: writer.writerow(["workers", str(dict(row))])
                        
                        from bot.setup import bot
                        from config import ADMIN_ID, ARCHIVE_GROUP_ID
                        from aiogram.types import BufferedInputFile
                        
                        target_chat = ARCHIVE_GROUP_ID if ARCHIVE_GROUP_ID else ADMIN_ID
                        file_data = csv_file.getvalue().encode('utf-8-sig')
                        date_str = datetime.now().strftime('%Y-%m-%d')
                        document = BufferedInputFile(file_data, filename=f"Archive_Cleanup_{date_str}.csv")
                        
                        await bot.send_document(
                            chat_id=target_chat,
                            document=document,
                            caption=f"🧹 **تقرير المنظف التلقائي**\nتم أرشفة وحذف `{total_records}` سجل قديم."
                        )
                    except Exception as telegram_error:
                        logging.error(f"⚠️ فشل إرسال الأرشيف لتيليجرام (سيتم الحذف لتوفير المساحة): {telegram_error}")
                
                # 🛡️ 3. الحذف النهائي (نفتح اتصالاً جديداً سريعاً للحذف فقط)
                if total_records > 0:
                    async with pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute("DELETE FROM admin_logs WHERE id IN (SELECT id FROM admin_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000)")
                            await conn.execute("DELETE FROM fraud_logs WHERE id IN (SELECT id FROM fraud_logs WHERE created_at < NOW() - INTERVAL '30 days' LIMIT 5000)")
                            await conn.execute("DELETE FROM chat_messages WHERE id IN (SELECT id FROM chat_messages WHERE created_at < NOW() - INTERVAL '60 days' LIMIT 5000)")
                            await conn.execute("DELETE FROM store_products WHERE id IN (SELECT id FROM store_products WHERE is_deleted = TRUE LIMIT 5000)")
                            await conn.execute("DELETE FROM workers WHERE id IN (SELECT id FROM workers WHERE is_deleted = TRUE LIMIT 5000)")
                        
                    logging.info(f"🧹 [المنظف التلقائي]: تم حذف {total_records} سجل بنجاح.")
                        
        except Exception as e:
            logging.error(f"❌ [المنظف التلقائي]: حدث خطأ فادح أثناء التنظيف: {e}")
        
        # الانتظار لمدة 24 ساعة
        await asyncio.sleep(86400)

async def run_daily_tasks():
    """الحارس الليلي: يعمل كل دقيقة ليفحص الوقت وينفذ المهام التلقائية"""
    await asyncio.sleep(60)
    while True:
        try:
            now = datetime.now()
            
            # 1. التقفيل التلقائي للوردية (الساعة 11:55 مساءً)
            if now.hour == 23 and now.minute == 55:
                if pool:
                    # 🛡️ جلب قائمة البقالات وإغلاق الاتصال فوراً
                    async with pool.acquire() as conn:
                        stores = await conn.fetch("SELECT id FROM stores WHERE status = 'active'")
                        
                    from services.accounting import generate_and_send_z_report
                    for s in stores:
                        # عملية التقفيل تفتح اتصالها الخاص بداخلها فلا داعي لاحتكار الاتصال هنا
                        await generate_and_send_z_report(s['id'], "غير محدد (تقفيل آلي)", is_auto=True)
                        await asyncio.sleep(1)
                await asyncio.sleep(60)

            # 2. النسخ الاحتياطي الشامل (الساعة 11:59 مساءً)
            if now.hour == 23 and now.minute == 59:
                if pool:
                    # 🛡️ جلب البقالات وإغلاق الاتصال
                    async with pool.acquire() as conn:
                        stores = await conn.fetch("SELECT id, name, telegram_id, archive_group_id FROM stores WHERE status = 'active'")
                                            
                    for store in stores:
                        backup_data = {}
                        
                        # 🛡️ نفتح الاتصال لثانية واحدة لجلب بيانات البقالة ثم نغلقه فوراً
                        async with pool.acquire() as conn:
                            backup_data['customers'] = [dict(r) for r in await conn.fetch("SELECT * FROM customers WHERE store_id = $1", store['id'])]
                            backup_data['products'] = [dict(r) for r in await conn.fetch("SELECT * FROM store_products WHERE store_id = $1", store['id'])]
                        
                        # 🛡️ العمليات البطيئة (تجهيز الملف وإرساله لتيليجرام) تحدث خارج اتصال قاعدة البيانات
                        import json
                        from aiogram.types import BufferedInputFile
                        from bot.setup import bot
                        
                        def json_serial(obj):
                            from datetime import datetime, date
                            from decimal import Decimal
                            if isinstance(obj, (datetime, date)): return obj.isoformat()
                            if isinstance(obj, Decimal): return float(obj)
                            raise TypeError("Type not serializable")
                            
                        json_str = json.dumps(backup_data, default=json_serial, ensure_ascii=False, indent=2)
                        file_data = json_str.encode('utf-8')
                        date_str = now.strftime('%Y-%m-%d')
                        document = BufferedInputFile(file_data, filename=f"Backup_{store['name']}_{date_str}.json")
                        
                        target_chat = store['archive_group_id'] if store['archive_group_id'] else store['telegram_id']
                        if target_chat:
                            try:
                                await bot.send_message(target_chat, f"💾 **النسخة الاحتياطية الشاملة**\nمرفق نسخة كاملة لبيانات البقالة.", parse_mode="Markdown")
                                await bot.send_document(chat_id=target_chat, document=document)
                            except Exception: 
                                pass
                                
                        # راحة بسيطة بين كل بقالة لتجنب حظر تيليجرام
                        await asyncio.sleep(2)
                        
                # ننتظر 60 ثانية لكي لا تتكرر العملية عدة مرات في نفس الدقيقة 11:59
                await asyncio.sleep(60)

            # ==========================================================
            # 🛑 3. الحارس الآلي (الإيقاف الصارم والتنبيهات المتصاعدة)
            # ==========================================================
            # سنقوم بتشغيل هذا الفحص كل يوم الساعة 10:00 صباحاً 
            if now.hour == 10 and now.minute == 0:
                if pool:
                    async with pool.acquire() as conn:
                        from bot.setup import bot
                        
                        # أ. الإيقاف التلقائي الصارم (Auto-Suspend) للبقالات المنتهية
                        expired_stores = await conn.fetch("SELECT id, name, telegram_id FROM stores WHERE status = 'active' AND trial_ends_at < NOW()")
                        for st in expired_stores:
                            await conn.execute("UPDATE stores SET status = 'suspended', suspend_reason = 'انتهاء فترة الاشتراك' WHERE id = $1", st['id'])
                            if st['telegram_id']:
                                try:
                                    await bot.send_message(st['telegram_id'], f"🛑 **إشعار إيقاف النظام**\n\nعفواً، انتهت فترة اشتراك بقالة ({st['name']}) وتم إيقاف النظام آلياً.\n\nيرجى التواصل مع الإدارة لتجديد الاشتراك واستعادة الخدمة فوراً.")
                                except: pass
                        
                        # ب. التنبيهات الآلية المتصاعدة (Automated Dunning)
                        # جلب البقالات التي سينتهي اشتراكها خلال 3 أيام أو أقل
                        warning_stores = await conn.fetch("""
                            SELECT id, name, telegram_id, DATE_PART('day', trial_ends_at - NOW()) as days_left 
                            FROM stores 
                            WHERE status = 'active' AND trial_ends_at > NOW() AND trial_ends_at <= NOW() + INTERVAL '3 days'
                        """)
                        
                        for st in warning_stores:
                            days = int(st['days_left'])
                            if days in [3, 1, 0]: # التنبيه قبل 3 أيام، ويوم، وفي نفس اليوم
                                day_word = "أيام" if days == 3 else ("يوم واحد" if days == 1 else "أقل من 24 ساعة")
                                msg = f"⚠️ **تنبيه اقتراب انتهاء الاشتراك**\n\nعزيزي صاحب بقالة ({st['name']})،\nتبقى **{day_word}** على انتهاء اشتراكك في النظام.\n\nيرجى التجديد قريباً لتجنب توقف الخدمة عنك وعن زبائنك."
                                if st['telegram_id']:
                                    try: await bot.send_message(st['telegram_id'], msg)
                                    except: pass

                    await asyncio.sleep(60) # لمنع التكرار في نفس الدقيقة
                
        except Exception as e:
            logging.error(f"Daily Tasks Error: {e}")
            
        # فحص الوقت كل 30 ثانية
        await asyncio.sleep(30)
