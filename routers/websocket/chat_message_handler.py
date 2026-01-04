# backend/services/chat_handler.py
from bson import ObjectId
from datetime import datetime, timezone
import asyncio
from database_clients.database_redis import get_redis
from messages_sever_processing.message_anki_validation import validate_anki_message
from messages_sever_processing.search_service import index_message

async def handle_chat_message(user: str, data: dict, db, manager):
    """
    Handles standard chat messages: persistence, Anki checks, broadcasting.
    """
    conversation_id = data.get("conversation_id")
    content = data.get("content")
    deck_name = data.get("deck_name")

    if not conversation_id or not content:
        return

    # 1. Fetch conversation (Needed for participants)
    conversation = await db.conversations.find_one({
        "_id": ObjectId(conversation_id)
    })

    if not conversation:
        return

    participants = conversation.get("participants", [])

    # 2. Anki Validation Trigger (Async)
    if deck_name:
        asyncio.create_task(
            validate_anki_message(
                user=user,
                content=content,
                deck_name=deck_name,
                participants=participants,
                manager=manager
            )
        )

    now = datetime.now(timezone.utc)

    # 3. Persist Message
    msg_entry = {
        "conversation_id": ObjectId(conversation_id),
        "sender": user,
        "content": content,
        "timestamp": now
    }
    insert_result = await db.messages.insert_one(msg_entry)
    new_message_id = str(insert_result.inserted_id)

    # 4. Update Conversation Stats (Unread counts, Last message)
    # Note: You could extract this into a separate 'update_conversation_stats' helper function
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
    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {
            "$set": {
                "last_message_at": now,
                "last_message_preview": preview
            }
        }
    )

    # 5. Semantic Search Indexing (Async)
    asyncio.create_task(
        index_message(
            mongo_id=new_message_id,
            content=content,
            conversation_id=conversation_id,
            sender=user,
            timestamp=now
        )
    )

    # 6. Broadcast
    # IMPORTANT: Ensure 'type' is included so the Client Dispatcher knows what to do
    message_payload = {
        "type": "chat_message",  # <--- Standardize this
        "conversation_id": conversation_id,
        "from": user,
        "content": content,
        "timestamp": now.isoformat()[:23]
    }

    await manager.broadcast_to_participants(
        participants,
        message_payload,
        sender=user
    )

    # 7. Invalidate Caches
    redis = await get_redis()
    await redis.delete(f"chat_history:{conversation_id}")
    for participant in participants:
        await redis.delete(f"user_conversations:{participant}")