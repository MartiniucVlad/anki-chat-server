# backend/services/chat_handler.py
from bson import ObjectId
from datetime import datetime, timezone
import asyncio
from database_clients.database_redis import get_redis
from messages_sever_processing.message_anki_processing import validate_anki_message
from messages_sever_processing.semantic_search_messages import index_message

async def handle_chat_message(user: str, data: dict, db, manager):
    conversation_id = data.get("conversation_id")
    content = data.get("content", "")
    deck_name = data.get("deck_name")
    story_attachment = data.get("story_attachment")  # None or {story_id, title, difficulty_label, chunk_count}

    if not conversation_id or (not content and not story_attachment):
        return

    conversation = await db.conversations.find_one({"_id": ObjectId(conversation_id)})
    if not conversation:
        return

    participants = conversation.get("participants", [])
    now = datetime.now(timezone.utc)

    # 2. Persist Message
    msg_entry = {
        "conversation_id": ObjectId(conversation_id),
        "sender": user,
        "content": content,
        "timestamp": now,
    }
    if story_attachment:
        msg_entry["story_attachment"] = {
            "story_id": story_attachment["story_id"],
            "title": story_attachment["title"],
            "difficulty_label": story_attachment["difficulty_label"],
            "chunk_count": story_attachment["chunk_count"],
        }

    insert_result = await db.messages.insert_one(msg_entry)
    message_id = str(insert_result.inserted_id)

    # 3. Anki Validation (only if there's text content to validate)
    if deck_name and content:
        asyncio.create_task(
            validate_anki_message(
                message_id=message_id,
                user=user,
                content=content,
                deck_name=deck_name,
                participants=participants,
                manager=manager
            )
        )

    # 4. Update Conversation Stats
    for participant in participants:
        if participant == user:
            continue
        await db.conversation_states.update_one(
            {"conversation_id": ObjectId(conversation_id), "user": participant},
            {"$inc": {"unread_count": 1}, "$set": {"updated_at": now}},
            upsert=True
        )
    preview = ""
    # Preview: prefer text content, fall back to story title
    if content:
        preview = content if len(content) <= 30 else content[:30] + "..."
    elif story_attachment:
        preview = f" {story_attachment['title']}"

    await db.conversations.update_one(
        {"_id": ObjectId(conversation_id)},
        {"$set": {"last_message_at": now, "last_message_preview": preview}}
    )

    # 5. Semantic indexing (only meaningful for text content)
    if content:
        asyncio.create_task(
            index_message(
                message_id=message_id,
                content=content,
                conversation_id=conversation_id,
                sender=user,
                timestamp=now
            )
        )

    # 6. Broadcast — include story_attachment so recipient renders it immediately
    message_payload = {
        "type": "chat_message",
        "message_id": message_id,
        "conversation_id": conversation_id,
        "from": user,
        "content": content,
        "timestamp": now.isoformat()[:23],
        "story_attachment": story_attachment,  # None if no attachment — frontend handles both
    }

    await manager.broadcast_to_participants(participants, message_payload, sender=user)

    # 7. Invalidate Caches
    redis = await get_redis()
    await redis.delete(f"chat_history:{conversation_id}")
    for participant in participants:
        await redis.delete(f"user_conversations:{participant}")