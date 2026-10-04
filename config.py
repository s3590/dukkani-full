import os
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials

# تحميل المتغيرات من ملف .env الموجود في مجلد المشروع
load_dotenv()

# ================= إعدادات قاعدة البيانات =================
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'DATABASE_URL'")

# ================= إعدادات البوت والمدير =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'BOT_TOKEN'")

ADMIN_ID_STR = os.getenv("ADMIN_ID")
if not ADMIN_ID_STR or ADMIN_ID_STR == "0":
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'ADMIN_ID'")
ADMIN_ID = int(ADMIN_ID_STR)

ARCHIVE_GROUP_ID = os.getenv("ARCHIVE_GROUP_ID")
if ARCHIVE_GROUP_ID:
    try:
        ARCHIVE_GROUP_ID = int(ARCHIVE_GROUP_ID.strip())
    except ValueError:
        ARCHIVE_GROUP_ID = None

# ================= حماية الجلسات (JWT) =================
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'JWT_SECRET'")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    raise ValueError("❌ خطأ حرج: لم يتم العثور على 'ADMIN_PASSWORD'")

# ================= إعدادات رديس (Redis) =================
REDIS_URL = os.getenv("REDIS_URL")

# ================= 🚀 تهيئة Firebase للإشعارات (من Render Secret Files) =================
# البحث عن الملف السري في المجلد الرئيسي أو في مجلد أسرار Render
firebase_paths = [
    "firebase-adminsdk.json",                  # المجلد الرئيسي
    "/etc/secrets/firebase-adminsdk.json"      # مجلد أسرار Render
]

firebase_cert_path = None
for path in firebase_paths:
    if os.path.exists(path):
        firebase_cert_path = path
        break

if firebase_cert_path:
    try:
        cred = credentials.Certificate(firebase_cert_path)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print(f"✅ تم تهيئة Firebase بنجاح من المسار: {firebase_cert_path}")
    except Exception as e:
        print(f"❌ خطأ في تهيئة Firebase: {e}")
else:
    print("⚠️ تحذير: لم يتم العثور على ملف 'firebase-adminsdk.json' السري في Render. الإشعارات لن تعمل!")
