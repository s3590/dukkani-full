import json
import logging
import asyncio
from aiohttp import web
import redis.asyncio as redis
import jwt
from config import REDIS_URL, JWT_SECRET
import database
from api.middlewares import validate_telegram_data

# قواميس لحفظ الاتصالات الحية
connected_stores = {}    # {store_id: [ws1, ws2]}
connected_customers = {} # {customer_id: [ws1, ws2]}
redis_client = None

async def start_redis_listener(app ):
    """تهيئة رديس وتشغيل مستمع اللاسلكي كمهمة خلفية"""
    global redis_client
    if not REDIS_URL:
        logging.warning("⚠️ لم يتم العثور على REDIS_URL، الإشعارات الحية ستعمل على نواة واحدة فقط.")
        return

    redis_client = redis.from_url(REDIS_URL)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("dukkani_events")
    logging.info("📡 تم الاتصال بـ Redis بنجاح وبدء الاستماع للإشعارات...")
    
    async def listen_loop():
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = json.loads(message["data"])
                    event_type = data.get("type")
                    
                    if event_type in ["new_order", "new_chat_message"]:
                        store_id = data.get("store_id")
                        if store_id in connected_stores:
                            msg_str = json.dumps({"type": event_type, "data": data.get("data")})
                            for ws in connected_stores[store_id]:
                                try: await ws.send_str(msg_str)
                                except Exception: pass
                                
                    elif event_type == "customer_update":
                        customer_id = data.get("customer_id")
                        if customer_id in connected_customers:
                            msg_str = json.dumps({"type": "order_update"})
                            for ws in connected_customers[customer_id]:
                                try: await ws.send_str(msg_str)
                                except Exception: pass
        except asyncio.CancelledError:
            await pubsub.unsubscribe("dukkani_events")
            await redis_client.close()
            logging.info("🛑 تم إغلاق اتصال Redis بأمان.")

    app['redis_listener_task'] = asyncio.create_task(listen_loop())

async def websocket_handler(request: web.Request):
    """مسار شاشة الطلبات الحية للبقالة (محصن)"""
    ws = web.WebSocketResponse(heartbeat=15.0) # 👈 يجعل الاتصال نشطاً وسريع الاستجابة دائماً    
    await ws.prepare(request)
    
    store_id = request.query.get('store_id')
    token = request.query.get('token')
    auth_type = request.query.get('auth_type', 'jwt')
    
    # 🛡️ حماية من الـ ValueError إذا كان الـ ID نصاً (مثل undefined أو NaN من المتصفح)
    if not store_id or not str(store_id).isdigit() or not token:
        await ws.close()
        return ws
        
    store_id = int(store_id)
    is_authorized = False
    
    # 🛡️ التحقق من الهوية (بقال/عامل عبر تيليجرام، أو مدير عبر وضع الظل)
    if auth_type == 'tma':
        user_data = validate_telegram_data(token)
        if user_data and 'id' in user_data:
            async with database.pool.acquire() as conn:
                real_store_id = await conn.fetchval("SELECT id FROM stores WHERE telegram_id = $1", user_data['id'])
                if not real_store_id:
                    real_store_id = await conn.fetchval("SELECT store_id FROM workers WHERE telegram_id = $1", user_data['id'])
                if real_store_id == store_id:
                    is_authorized = True
    else:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            if payload.get('role') == 'super_admin' or payload.get('store_id') == store_id:
                is_authorized = True
        except Exception:
            pass

    if not is_authorized:
        await ws.close()
        return ws
    
    request.app['websockets'].add(ws)
    if store_id not in connected_stores: connected_stores[store_id] = []
    connected_stores[store_id].append(ws)
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT and msg.data == 'ping':
                await ws.send_str('pong')
    finally:
        connected_stores[store_id].remove(ws)
        if not connected_stores[store_id]: del connected_stores[store_id]
        request.app['websockets'].discard(ws)
        
    return ws

async def customer_websocket_handler(request: web.Request):
    """مسار الرادار الحي للزبون (محصن)"""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)
    
    token = request.query.get('token')
    if not token:
        await ws.close()
        return ws
        
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
        customer_id = payload.get('customer_id')
        if not customer_id: raise ValueError()
    except Exception:
        await ws.close()
        return ws
        
    request.app['websockets'].add(ws)
    if customer_id not in connected_customers: connected_customers[customer_id] = []
    connected_customers[customer_id].append(ws)
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT and msg.data == 'ping':
                await ws.send_str('pong')
    finally:
        connected_customers[customer_id].remove(ws)
        if not connected_customers[customer_id]: del connected_customers[customer_id]
        request.app['websockets'].discard(ws)
        
    return ws

# --- دوال النشر (Publishers) ---
async def notify_store_new_order(store_id: int, order_data: dict):
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "new_order", "store_id": store_id, "data": order_data}))
    elif store_id in connected_stores:
        msg_str = json.dumps({"type": "new_order", "data": order_data})
        for ws in connected_stores[store_id]:
            try: await ws.send_str(msg_str)
            except Exception: pass

async def notify_store_new_chat_message(store_id: int, chat_data: dict):
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "new_chat_message", "store_id": store_id, "data": chat_data}))
    elif store_id in connected_stores:
        msg_str = json.dumps({"type": "new_chat_message", "data": chat_data})
        for ws in connected_stores[store_id]:
            try: await ws.send_str(msg_str)
            except Exception: pass

async def notify_customer_order_update(customer_id: int):
    """إشعار الزبون بتحديث حالة طلبه"""
    if redis_client:
        await redis_client.publish("dukkani_events", json.dumps({"type": "customer_update", "customer_id": customer_id}))
    elif customer_id in connected_customers:
        msg_str = json.dumps({"type": "order_update"})
        for ws in connected_customers[customer_id]:
            try: await ws.send_str(msg_str)
            except Exception: pass

async def notify_admin_new_log(log_data: dict):
    pass

def setup_websocket_routes(app: web.Application):
    app.router.add_get('/ws/live_orders', websocket_handler)
    app.router.add_get('/ws/customer_updates', customer_websocket_handler) # 👈 المسار الجديد للزبائن
    app.on_startup.append(start_redis_listener)
