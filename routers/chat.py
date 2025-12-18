# backend/routers/chat.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from websocket_manager import manager
from jose import jwt, JWTError
from security import SECRET_KEY, ALGORITHM, get_current_user
from database import get_db
from datetime import datetime, timezone
from models import MessageInDB
from pymongo.database import Database as PyMongoDatabase


router = APIRouter(tags=["Chat"])


# --- Helper to validate Token via Query Param ---
async def get_current_user_ws(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        return username
    except JWTError:
        return None


# --- Helper: Find or Create Conversation ---
async def get_or_create_conversation_id(user1: str, user2: str, db: PyMongoDatabase) -> str:
    # 1. Sort names to ensure "alice-bob" is same as "bob-alice"
    participants = sorted([user1, user2])

    # 2. Try to find existing conversation
    existing_conv = await db.conversations.find_one({
        "participants": participants,
        "type": "private"
    })

    if existing_conv:
        return str(existing_conv["_id"])

    # 3. Create new if not exists
    new_conv = await db.conversations.insert_one({
        "participants": participants,
        "type": "private",
        "created_at": datetime.utcnow()
    })
    return str(new_conv.inserted_id)


# --- The WebSocket Endpoint ---
@router.websocket("/ws/chat")
async def websocket_endpoint(
        websocket: WebSocket,
        token: str = Query(...),
        db: PyMongoDatabase = Depends(get_db)  # We need DB access now
):
    user = await get_current_user_ws(token)
    if user is None:
        await websocket.close(code=4003)
        return

    await manager.connect(websocket, user)

    try:
        while True:
            data = await websocket.receive_json()
            target_user = data.get("to")
            content = data.get("content")

            if target_user and content:
                # 1. Get the Conversation ID (The Bridge!)
                conversation_id = await get_or_create_conversation_id(user, target_user, db)

                # 2. Save to MongoDB (The Persistence)
                msg_entry = MessageInDB(
                    conversation_id=conversation_id,
                    sender=user,
                    content=content
                )
                await db.messages.insert_one(msg_entry.model_dump())

                # 3. Send to Recipient (The Real-time)
                # We format it nicely for the frontend
                message_payload = {
                    "from": user,
                    "content": content,
                    "conversation_id": conversation_id,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }

                await manager.send_personal_message(message_payload, target_user)


    except WebSocketDisconnect:
        manager.disconnect(websocket, user)


from fastapi import HTTPException

@router.get("/chat/history/{friend_username}")
async def get_chat_history(
    friend_username: str,
    current_user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    # 1. Conversation participants (same logic as WS)
    participants = sorted([current_user, friend_username])

    conversation = await db.conversations.find_one({
        "participants": participants,
        "type": "private"
    })

    if not conversation:
        # No chat yet → empty history
        return []

    conversation_id = str(conversation["_id"])

    # 2. Fetch messages
    cursor = db.messages.find(
        {"conversation_id": conversation_id}
    ).sort("timestamp", 1)

    messages = []
    async for msg in cursor:
        messages.append({
            "from": msg["sender"],
            "content": msg["content"],
            "timestamp": msg["timestamp"].isoformat()
        })

    return messages
