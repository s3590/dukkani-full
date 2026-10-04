# مسار الملف: bot/handlers/worker.py
from aiogram import Router, F, types
import database
from bot.setup import bot

worker_router = Router()

# =====================================================================
# 👷‍♂️ دخول العمال بالكود السري (5 أرقام)
# =====================================================================
@worker_router.message(F.text.regexp(r'^\d{5}$'))
async def worker_login(message: types.Message):
    passcode = message.text
    async with database.pool.acquire() as conn:
        # 🛡️ 1. جلب بيانات العامل، واسم البقالة، وآيدي المدير في استعلام واحد (JOIN) لتسريع الأداء
        worker = await conn.fetchrow("""
            SELECT w.id, w.name, w.telegram_id as old_tg_id, 
                   s.name as store_name, s.telegram_id as admin_tg_id 
            FROM workers w
            JOIN stores s ON w.store_id = s.id
            WHERE w.passcode = $1 AND w.is_deleted = FALSE
        """, passcode)

        if worker:
            # ربط حساب التليجرام بالعامل
            await conn.execute("UPDATE workers SET telegram_id = $1 WHERE id = $2", message.from_user.id, worker['id'])
            await message.answer(f"✅ أهلاً بك يا {worker['name']}!\nتم ربط حسابك بنجاح كعامل في {worker['store_name']}.\nاضغط /start لفتح القائمة الخاصة بك.")
            
            # 🛡️ 2. حماية من الاختراق: إرسال إشعار للمدير إذا قام شخص جديد بربط الحساب
            if worker['admin_tg_id'] and worker['old_tg_id'] != message.from_user.id:
                try:
                    await bot.send_message(
                        worker['admin_tg_id'], 
                        f"🚨 **إشعار أمني:**\nتم للتو تسجيل دخول العامل ({worker['name']}) من حساب تيليجرام جديد.\nإذا لم تكن على علم بذلك، قم بتغيير كوده السري فوراً من الإدارة."
                    )
                except Exception:
                    pass
        else:
            await message.answer("❌ الكود السري غير صحيح أو تم إيقاف حسابك.")
