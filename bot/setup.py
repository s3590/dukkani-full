# مسار الملف: bot/setup.py
import logging
import traceback
from aiogram import Bot, Dispatcher, types
from config import BOT_TOKEN, ADMIN_ID

# إعداد تسجيل الأخطاء
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# صائد الأخطاء الشامل للبوت
@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    err_str = traceback.format_exc()
    logging.error(f"Bot Error: {err_str}")
    try:
        # 🛡️ استخدام HTML بدلاً من Markdown لتجنب انهيار رسالة الخطأ بسبب الرموز البرمجية
        safe_err = err_str.replace('<', '&lt;').replace('>', '&gt;')
        await bot.send_message(
            ADMIN_ID, 
            f"🚨 <b>خطأ في البوت:</b>\n\n<pre><code class='language-python'>{safe_err[:3800]}</code></pre>", 
            parse_mode="HTML"
        )
    except: 
        pass

# استيراد الراوترات لتجنب الاستدعاء الدائري (Circular Import)
from bot.handlers.admin import admin_router
from bot.handlers.worker import worker_router
from bot.handlers.store import store_router

# تسجيل الراوترات في الموزع (Dispatcher)
dp.include_router(admin_router)
dp.include_router(worker_router)
dp.include_router(store_router)
