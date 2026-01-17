# backend/routers/chat.py

from fastapi import APIRouter

from messages_sever_processing.semantic_search_messages import search_similar_messages
from security import get_current_user
from database_clients.database_mongo import get_db
from models import CreateConversationRequest, ConversationSummary
from pymongo.database import Database as PyMongoDatabase
import json
from database_clients.database_redis import get_redis
from bson import ObjectId
from datetime import datetime, timezone
from fastapi import Depends, HTTPException


router = APIRouter(tags=["Chat"])

# --- Helper: Find or Create Conversation ---
# async def get_or_create_conversation_id(user1: str, user2: str, db: PyMongoDatabase) -> str:
#     # 1. Sort names to ensure "alice-bob" is same as "bob-alice"
#     participants = sorted([user1, user2])
#
#     # 2. Try to find existing conversation
#     existing_conv = await db.conversations.find_one({
#         "participants": participants,
#         "type": "private"
#     })
#
#     if existing_conv:
#         return str(existing_conv["_id"])
#
#     # 3. Create new if not exists
#     new_conv = await db.conversations.insert_one({
#         "participants": participants,
#         "type": "private",
#         "created_at": datetime.utcnow()
#     })
#     return str(new_conv.inserted_id)





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
        print(cached_history)
        # SECURITY CHECK: Even if cached, we must ensure the user is a participant.
        # This is a fast, lightweight query compared to fetching 100s of messages.
        conversation = await db.conversations.find_one(
            {"_id": ObjectId(conversation_id)},
            {"participants": 1}
        )

        # If conversation exists and user is in it, return cache
        if conversation and current_user in conversation.get("participants", []):
            print(f"Cache HIT: History for {conversation_id}")
            return json.loads(cached_history)

    print(f"Cache MISS: History for {conversation_id}")

    # --- 2. MONGO QUERY (Existing Logic) ---
    conversation = await db.conversations.find_one({
        "_id": ObjectId(conversation_id)
    })
    print(conversation)
    if not conversation:
        return []

    if current_user not in conversation.get("participants", []):
        raise HTTPException(status_code=403, detail="Not authorized to view this chat")

    cursor = db.messages.find(
        {"conversation_id": ObjectId(conversation_id)}
    ).sort("timestamp", 1)

    messages = []
    async for msg in cursor:
        msg_obj = {
            "message_id": str(msg["_id"]),
            "sender": msg["sender"],
            "content": msg["content"],
            "timestamp": msg["timestamp"].isoformat()[:23],
            "anki_review": None
        }

        # If the DB has the review, map it to your frontend structure
        if "anki_review" in msg:
            db_review = msg["anki_review"]
            msg_obj["anki_review"] = {
                "tickedNotes": db_review.get("ticked_notes", []),
                "messageReview": db_review.get("message_review", ""),
                "deckName": db_review.get("deck_name", ""),
            }

        messages.append(msg_obj)

    # --- 3. SAVE TO REDIS ---
    await redis.set(cache_key, json.dumps(messages), ex=3600)

    return messages


@router.post("/chat/conversations/initiate")
async def initiate_conversation(
        req: CreateConversationRequest,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):

    #invalidate redis current conversation list
    redis = await get_redis()
    await redis.delete(f"user_conversations:{current_user}")


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
            "admins": participants,
            "type": "private",
            "created_at": datetime.now(timezone.utc)
        })
        return {"conversation_id": str(new_conv.inserted_id)}

    # Logic for Group Chat - Always creates new
    else:
        new_group = await db.conversations.insert_one({
            "participants": req.participants,
            "admins" : [current_user],
            "type": "group",
            "name": req.group_name,
            "created_at": datetime.now(timezone.utc)
        })
        return {"conversation_id": str(new_group.inserted_id)}


from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId


# ... other imports (get_db, get_current_user, get_redis, etc.)

@router.delete("/chat/conversations/{conversation_id}")
async def delete_conversation(
        conversation_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    # 1. Validate ObjectId format
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=400, detail="Invalid conversation ID")

    oid = ObjectId(conversation_id)

    # 2. Fetch the conversation
    conversation = await db.conversations.find_one({"_id": oid})

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # 3. Authorization Check
    # Ensure the admins field exists and current_user is in it
    admins = conversation.get("admins", [])

    if current_user not in admins:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this conversation")

    # 4. Delete Data (Conversation, Messages, and States)
    # A. Delete the conversation document
    delete_result = await db.conversations.delete_one({"_id": oid})

    if delete_result.deleted_count == 0:
        raise HTTPException(status_code=500, detail="Failed to delete conversation")

    # B. Delete all messages associated with this conversation
    await db.messages.delete_many({"conversation_id": oid})

    # C. Delete conversation states (unread counts/last read status for all users)
    await db.conversation_states.delete_many({"conversation_id": oid})

    # 5. Invalidate Redis Cache for ALL participants
    # Since the chat is deleted, it must disappear from everyone's sidebar immediately.
    redis = await get_redis()
    participants = conversation.get("participants", [])

    for p in participants:
        await redis.delete(f"user_conversations:{p}")

    return {"message": "Conversation and all associated data deleted successfully"}





    #invalidate redis current conversation list
    redis = await get_redis()
    await redis.delete(f"user_conversations:{current_user}")







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
            "admins" : conv["admins"],
            "type": conv["type"],
            "name": display_name,
            "created_at": serialize_date(conv["created_at"]),
            "last_message_preview": conv.get("last_message_preview"),
            "last_message_at": serialize_date(conv.get("last_message_at")),
            "unread_count": unread_map.get(str(conv["_id"]), 0)
        })
        print(conv["admins"])

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


@router.get("/chat/search/semantic")
async def search_messages(
        query: str,
        conversation_id: str = None,  # Optional: leave empty to search ALL chats
        current_user: str = Depends(get_current_user)
):
    if not query:
        return []

    results = await search_similar_messages(
        query=query,
        limit=5,
        conversation_id=conversation_id
    )

    return results