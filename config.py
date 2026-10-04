import json
import os

from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials

# Load project environment variables.
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

# ================= Firebase initialization =================
def _load_firebase_credentials():
    """Load Firebase credentials from env var first, then secret file paths."""
    raw = os.getenv("FIREBASE_CREDENTIALS")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("type") and data.get("client_email"):
                return data
        except Exception as exc:
            print(f"⚠️ Firebase env JSON is invalid: {exc}")

    firebase_paths = [
        "firebase-adminsdk.json",
        "/etc/secrets/firebase-adminsdk.json",
    ]

    for path in firebase_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and data.get("type") and data.get("client_email"):
                    return data
            except Exception as exc:
                print(f"⚠️ Failed to read Firebase secret from {path}: {exc}")

    return None

firebase_credentials = _load_firebase_credentials()
if firebase_credentials:
    try:
        cred = credentials.Certificate(firebase_credentials)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print("✅ تم تهيئة Firebase بنجاح.")
    except Exception as e:
        print(f"❌ خطأ في تهيئة Firebase: {e}")
else:
    print("⚠️ تحذير: لم يتم العثور على بيانات Firebase válidas. الإشعارات لن تعمل حتى يتم تجهيز المتغيرات أو الملفات السرية.")
