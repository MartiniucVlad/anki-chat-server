import json
from datetime import datetime, timezone
from database_clients.database_redis import get_redis


async def validate_anki_message(user: str, content: str, deck_name: str, participants: list, manager):
    """
    Validates message content against the user's active Anki session
    and broadcasts success events to ALL participants in the chat.
    """
    redis = await get_redis()

    # Sanitize key
    safe_deck_name = deck_name.replace(" ", "_")
    redis_key = f"anki_session:{user}:{safe_deck_name}"

    # 1. FETCH STATE
    raw_data = await redis.get(redis_key)
    if not raw_data:
        return

    session_data = json.loads(raw_data)
    notes = session_data.get("notes", [])

    state_modified = False
    newly_reviewed_ids = []

    # 2. VALIDATION LOOP
    for note in notes:
        # Skip cards already reviewed
        if note.get("is_reviewed"):
            continue

        card_front = note.get("front", "").strip()

        # --- Simple Check ---
        # Checks if the word exists in the message (Case insensitive)
        is_match = False
        if card_front and card_front.lower() in content.lower():
            is_match = True

        # 3. RECORD MATCH
        if is_match:
            note["is_reviewed"] = True
            newly_reviewed_ids.append({
                "id": note["id"],
                "word": card_front
            })
            state_modified = True
            print(f" {user} matched word: {card_front}")

    # 4. SAVE & BROADCAST
    if state_modified:
        # A. Update Redis
        session_data["notes"] = notes
        await redis.set(redis_key, json.dumps(session_data), ex=86400)

        # B. Broadcast to Everyone
        payload = {
            "type": "learning_update",
            "ticked_notes": newly_reviewed_ids,
            "message_review" : "testString", # Ai review that points out errors  in the user's message
            "deck_name": deck_name,
            "learner": user,
            "timestamp": str(datetime.now(timezone.utc))
        }
        print(payload)
        # Using the broadcast method as requested
        await manager.broadcast_to_participants(
            participants=participants,
            message=payload,
            sender=user
        )