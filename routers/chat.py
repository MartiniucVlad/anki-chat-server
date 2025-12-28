# backend/routers/chat.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from websocket_manager import manager
from jose import jwt, JWTError
from security import SECRET_KEY, ALGORITHM, get_current_user
from database_mongo import get_db
from datetime import datetime, timezone
from models import MessageInDB, CreateConversationRequest, ConversationSummary
from pymongo.database import Database as PyMongoDatabase
from bson import ObjectId
import json
from database_redis import get_redis



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
from bson import ObjectId
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect, Query, Depends

@router.websocket("/ws/chat")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
    db: PyMongoDatabase = Depends(get_db)
):
    user = await get_current_user_ws(token)
    if not user:
        await websocket.close(code=4003)
        return

    await manager.connect(websocket, user)

    try:
        while True:
            data = await websocket.receive_json()

            # Expected: { "conversation_id": "...", "content": "..." }
            conversation_id = data.get("conversation_id")
            content = data.get("content")

            if not conversation_id or not content:
                continue

            # 1. Fetch conversation
            conversation = await db.conversations.find_one({
                "_id": ObjectId(conversation_id)
            })

            if not conversation:
                continue

            participants = conversation.get("participants", [])

            now = datetime.now(timezone.utc)

            # 2. Persist message
            msg_entry = {
                "conversation_id": ObjectId(conversation_id),
                "sender": user,
                "content": content,
                "timestamp": now
            }
            await db.messages.insert_one(msg_entry)
            # we update the number of unread messages for user
            for participant in participants:
                if participant == user:
                    continue
                await db.conversation_states.update_one(
                    {
                        "conversation_id": ObjectId(conversation_id),
                        "user": participant
                    },
                    {
                        "$inc": {"unread_count": 1},
                        "$set": {"updated_at": now}
                    },
                    upsert=True
                )

            preview = content if len(content) <= 10 else content[:10] + "..."
            # other useful info held by the conversation
            await db.conversations.update_one(
                {"_id": ObjectId(conversation_id)},
                {
                    "$set": {
                        "last_message_at": now,
                        "last_message_preview": preview
                    }
                }
            )

            # 4. Broadcast to participants
            message_payload = {
                "conversation_id": conversation_id,
                "from": user,
                "content": content,
                "timestamp": now.isoformat()
            }

            await manager.broadcast_to_participants(
                participants,
                message_payload,
                sender=user
            )

            redis = await get_redis()

            # Since the conversation list changed (new preview, new time, unread count),
            # we must delete the cache for EVERYONE in this chat.
            await redis.delete(f"chat_history:{conversation_id}")  # <--- ADD THIS LINE

            # 2. Invalidate Sidebar for participants
            for participant in participants:
                cache_key = f"user_conversations:{participant}"
                await redis.delete(cache_key)

    except WebSocketDisconnect:
        manager.disconnect(websocket, user)



from fastapi import HTTPException


@router.get("/chat/history/{conversation_id}")
async def get_chat_history(
        conversation_id: str,
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db),
):
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    # --- 1. REDIS CHECK ---
    redis = await get_redis()
    cache_key = f"chat_history:{conversation_id}"

    cached_history = await redis.get(cache_key)

    if cached_history:
        # SECURITY CHECK: Even if cached, we must ensure the user is a participant.
        # This is a fast, lightweight query compared to fetching 100s of messages.
        conversation = await db.conversations.find_one(
            {"_id": ObjectId(conversation_id)},
            {"participants": 1}
        )

        # If conversation exists and user is in it, return cache
        if conversation and current_user in conversation.get("participants", []):
            print(f"⚡ Cache HIT: History for {conversation_id}")
            return json.loads(cached_history)

    print(f"🐢 Cache MISS: History for {conversation_id}")

    # --- 2. MONGO QUERY (Existing Logic) ---
    conversation = await db.conversations.find_one({
        "_id": ObjectId(conversation_id)
    })

    if not conversation:
        return []

    if current_user not in conversation.get("participants", []):
        raise HTTPException(status_code=403, detail="Not authorized to view this chat")

    cursor = db.messages.find(
        {"conversation_id": ObjectId(conversation_id)}
    ).sort("timestamp", 1)

    messages = []
    async for msg in cursor:
        messages.append({
            "sender": msg["sender"],
            "content": msg["content"],
            "timestamp": msg["timestamp"].isoformat()
        })

    # --- 3. SAVE TO REDIS ---
    await redis.set(cache_key, json.dumps(messages), ex=3600)

    return messages


@router.post("/chat/conversations/initiate")
async def initiate_conversation(req: CreateConversationRequest, db: PyMongoDatabase = Depends(get_db)):
    # Logic for Private Chat (DM) - Idempotent
    if not req.is_group:
        # Sort to ensure uniqueness for DMs
        participants = sorted(req.participants)
        existing = await db.conversations.find_one({
            "participants": participants,
            "type": "private"
        })
        if existing:
            return {"conversation_id": str(existing["_id"])}

        # Create new DM
        new_conv = await db.conversations.insert_one({
            "participants": participants,
            "type": "private",
            "created_at": datetime.now(timezone.utc)
        })
        return {"conversation_id": str(new_conv.inserted_id)}

    # Logic for Group Chat - Always creates new (or checks custom logic)
    else:
        new_group = await db.conversations.insert_one({
            "participants": req.participants,
            "type": "group",
            "name": req.group_name,
            "created_at": datetime.now(timezone.utc)
        })
        return {"conversation_id": str(new_group.inserted_id)}


@router.get("/chat/conversations/list", response_model=list[ConversationSummary])
async def get_conversation_list(
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    # --- 1. REDIS CHECK ---
    redis = await get_redis()
    cache_key = f"user_conversations:{current_user}"

    # Try to fetch from memory first
    cached_data = await redis.get(cache_key)
    if cached_data:
        print("⚡ Cache HIT: Serving from Redis")
        return json.loads(cached_data)

    print("🐢 Cache MISS: Fetching from Mongo")

    # --- 2. EXISTING MONGO LOGIC (No changes needed here) ---
    cursor = db.conversations.find(
        {"participants": current_user}
    ).sort("last_message_at", -1)

    conversations = await cursor.to_list(length=None)

    conv_ids = [c["_id"] for c in conversations]

    states_cursor = db.conversation_states.find({
        "user": current_user,
        "conversation_id": {"$in": conv_ids}
    })
    states = await states_cursor.to_list(length=None)

    unread_map = {str(state["conversation_id"]): state.get("unread_count", 0) for state in states}

    response_list = []

    for conv in conversations:
        display_name = ""
        if conv.get("type") == "private":
            other_participants = [p for p in conv["participants"] if p != current_user]
            display_name = other_participants[0] if other_participants else "Me"
        else:
            display_name = conv.get("name", "Unnamed Group")

        # Helper to handle datetime serialization
        def serialize_date(d):
            return d.isoformat() if isinstance(d, datetime) else d

        response_list.append({
            "id": str(conv["_id"]),
            "participants": conv["participants"],
            "type": conv["type"],
            "name": display_name,
            "created_at": serialize_date(conv["created_at"]),
            "last_message_preview": conv.get("last_message_preview"),
            "last_message_at": serialize_date(conv.get("last_message_at")),
            "unread_count": unread_map.get(str(conv["_id"]), 0)
        })

    # --- 3. SAVE TO REDIS ---
    # Save the result for 1 hour (3600 seconds)
    await redis.set(cache_key, json.dumps(response_list), ex=3600)

    return response_list


@router.post("/chat/conversations/{conv_id}/read")
async def mark_read(conv_id: str, user=Depends(get_current_user), db: PyMongoDatabase = Depends(get_db)):
    now = datetime.now(timezone.utc)

    await db.conversation_states.update_one(
        {
            "conversation_id": ObjectId(conv_id),
            "user": user
        },
        {
            "$set": {
                "unread_count": 0,
                "last_read_at": now
            }
        },
        upsert=True
    )
    redis = await get_redis()
    await redis.delete(f"user_conversations:{user}")


