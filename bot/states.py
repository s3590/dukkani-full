# مسار الملف: bot/states.py
from aiogram.fsm.state import State, StatesGroup

# ==========================================
# 🏪 حالات صاحب البقالة
# ==========================================
class AddCustomer(StatesGroup):
    name = State()
    phone = State()
    password = State()
    balance = State()

class AddProduct(StatesGroup):
    name = State()
    price = State()
    photo = State()
    stock = State()
    barcode = State()

class ManualOrder(StatesGroup):
    select_customer = State()
    build_cart = State()
    cash_paid = State()

class AddPayment(StatesGroup):
    select_customer = State()
    amount = State()

class Support(StatesGroup):
    waiting_for_msg = State()
    waiting_for_reply = State()

class SendPromo(StatesGroup):
    waiting_for_promo_msg = State()

class RangeReport(StatesGroup):
    start_date = State()
    end_date = State()

class EditCreditLimit(StatesGroup):
    select_customer = State()
    new_limit = State()

# ==========================================
# 🆕 حالات تسجيل بقالة جديدة
# ==========================================
class RegisterStore(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()

# ==========================================
# 👑 حالات القيادة العليا (المدير العام)
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()

class AdminCatalog(StatesGroup):
    waiting_for_product_name = State()
    waiting_for_product_image = State()
