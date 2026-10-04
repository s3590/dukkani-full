# مسار الملف: run.py
import os
import logging
from aiohttp import web
from api.app import create_app
import config # 👈 استيراد الإعدادات لضمان تهيئة فايربيس والمتغيرات مبكراً وبأمان

# إعداد سجل الأخطاء الأساسي للسيرفر (تم إصلاح المسافة الزائدة )
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

if __name__ == "__main__":
    logging.info("🚀 جاري إشعال محركات دُكّاني...")
    
    # إنشاء تطبيق الويب (والذي يضم مسارات الـ API والـ Middlewares)
    app = create_app()
    
    # تشغيل السيرفر (والذي بدوره سيشغل البوت وقاعدة البيانات عبر on_startup)
    port = int(os.environ.get("PORT", 8080))
    logging.info(f"🌐 السيرفر سيعمل على المنفذ: {port}")
    
    # تشغيل السيرفر
    web.run_app(app, host='0.0.0.0', port=port)
