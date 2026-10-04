import os
from aiohttp import web

# 👇 ذاكرة مؤقتة لحفظ صفحات HTML لتسريع السيرفر مليون مرة 👇
TEMPLATE_CACHE = {}

def get_template(filename: str) -> str:
    """دالة ذكية تقرأ الملف مرة واحدة فقط وتحفظه في الذاكرة (مع حماية)"""
    if filename not in TEMPLATE_CACHE:
        filepath = os.path.join('templates', filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                TEMPLATE_CACHE[filename] = f.read()
        except FileNotFoundError:
            # 🛡️ إرجاع رسالة واضحة بدلاً من انهيار السيرفر بأكمله
            return f"<h1>عفواً، ملف {filename} غير موجود في السيرفر!</h1>"
            
    return TEMPLATE_CACHE[filename]

async def serve_index(request: web.Request):
    return web.Response(text=get_template('index.html'), content_type='text/html')

async def serve_pos(request: web.Request):
    return web.Response(text=get_template('pos.html'), content_type='text/html')

async def serve_manage(request: web.Request):
    return web.Response(text=get_template('manage.html'), content_type='text/html')

async def serve_admin(request: web.Request):
    return web.Response(text=get_template('admin.html'), content_type='text/html')

def setup_views(app: web.Application):
    """تسجيل مسارات الصفحات في التطبيق"""
    app.router.add_get('/', serve_index)
    app.router.add_get('/pos', serve_pos)
    app.router.add_get('/manage', serve_manage)
    app.router.add_get('/admin', serve_admin)
    
