# مسار الملف: bot/keyboards.py
import urllib.parse
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from config import ADMIN_ID

def get_main_keyboard(permissions=None):
    kb = []
    row1 = []
    if permissions is None or 'manage_customers' in permissions: row1.append(InlineKeyboardButton(text="👥 إضافة زبون", callback_data="add_customer"))
    if row1: kb.append(row1)
    
    row2 = []
    if permissions is None or 'pos' in permissions: row2.append(InlineKeyboardButton(text="🛒 تسجيل طلب يدوي", callback_data="manual_order"))
    if permissions is None or 'add_payment' in permissions: row2.append(InlineKeyboardButton(text="💰 تسجيل سداد", callback_data="add_payment"))
    if row2: kb.append(row2)
    
    row3 = []
    if permissions is None or 'view_reports' in permissions: 
        row3.append(InlineKeyboardButton(text="📊 مبيعات اليوم", callback_data="daily_report"))
        row3.append(InlineKeyboardButton(text="📅 مبيعات فترة", callback_data="range_report"))
    if row3: kb.append(row3)
    
    row4 = []
    if permissions is None or 'view_reports' in permissions: row4.append(InlineKeyboardButton(text="👤 كشف حساب زبون", callback_data="customer_statement"))
    if permissions is None: row4.append(InlineKeyboardButton(text="🔗 رابط تطبيقي", callback_data="my_app_link"))
    if row4: kb.append(row4)
    
    row5 = []
    if permissions is None or 'manage_customers' in permissions: row5.append(InlineKeyboardButton(text="⚙️ تعديل سقف الدين", callback_data="edit_credit_limit"))
    if row5: kb.append(row5)
    
    if permissions is None or 'manage_products' in permissions:
        kb.append([InlineKeyboardButton(text="🪄 تعبئة المتجر تلقائياً", callback_data="auto_fill_products")])
        
    # 🛡️ تعديل: إرسال العروض يفضل أن يكون لصاحب البقالة فقط (permissions is None)
    if permissions is None:
        kb.append([InlineKeyboardButton(text="📢 إرسال عرض للزبائن", callback_data="send_promo")])

    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_bottom_keyboard(user_id=None, permissions=None):
    if user_id == ADMIN_ID:
        # أزرار المدير العام 👑
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🎛️ غرفة العمليات"), KeyboardButton(text="📱 دخول التطبيق")],
                [KeyboardButton(text="🧪 اختبار الضغط"), KeyboardButton(text="☢️ فورمات النظام")]
            ],
            resize_keyboard=True, is_persistent=True
        )
    else:
        # أزرار صاحب البقالة / العامل 🏪
        keyboard = [[KeyboardButton(text="📱 بيانات الدخول للتطبيق")]]
        
        row2 = []
        # 🛡️ إصلاح: إظهار زر رفع الصورة فقط لمن يملك الصلاحية
        if permissions is None or 'manage_products' in permissions:
            row2.append(KeyboardButton(text="📦 رفع صورة منتج"))
            
        row2.append(KeyboardButton(text="🎧 الدعم الفني"))
        keyboard.append(row2)
        
        keyboard.append([KeyboardButton(text="🏠 الرئيسية"), KeyboardButton(text="❌ إلغاء")])
        
        return ReplyKeyboardMarkup(
            keyboard=keyboard,
            resize_keyboard=True, is_persistent=True
        )

def get_whatsapp_btn(phone, message):
    # 🛡️ إصلاح: التحقق من وجود رقم هاتف صالح قبل إنشاء الزر
    if not phone or phone == 'None' or str(phone).strip() == '':
        return None
        
    # تنظيف رقم الهاتف من أي مسافات أو رموز
    clean_phone = str(phone).replace('+', '').replace(' ', '')
    url = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(message )}"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📲 إرسال إشعار للزبون عبر واتساب", url=url)]
    ])
    
