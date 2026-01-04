# backend/routers/ws_hub.py
from fastapi import WebSocket, WebSocketDisconnect, Query, Depends, APIRouter
from routers.websocket.websocket_manager import manager
from security import get_current_user
from database_clients.database_mongo import get_db
from pymongo.database import Database as PyMongoDatabase
from routers.websocket.chat_message_handler import handle_chat_message



router = APIRouter(tags=["WebSocket"])



@router.websocket("/ws/hub")
async def websocket_hub(
        websocket: WebSocket,
        token: str = Query(...),
        db: PyMongoDatabase = Depends(get_db)
):
    user = await get_current_user(token)
    if not user:
        await websocket.close(code=4003)
        return

    await manager.connect(websocket, user)

    try:
        while True:
            data = await websocket.receive_json()

            # 1. The Dispatcher Logic
            # We expect every message to have a "type".
            # Default to "chat" for backward compatibility if needed, or enforce strict typing.
            msg_type = data.get("type", "chat_message")

            # 2. Route to Handlers
            if msg_type == "chat_message":
                await handle_chat_message(user, data, db, manager)

            elif msg_type == "notification_ack":
                # Example: User read a notification
                # await handle_notification_ack(user, data, db)
                pass

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                print(f"Unknown message type received from {user}: {msg_type}")

    except WebSocketDisconnect:
        manager.disconnect(websocket, user)
    except Exception as e:
        print(f"Critical WebSocket Error: {e}")
