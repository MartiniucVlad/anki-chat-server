import json
from datetime import datetime, timezone
from database_clients.database_redis import get_redis
# Import the helper we just created
from messages_sever_processing.llmvalidation import check_usage_with_siliconflow


async def validate_anki_message(message_id: str, user: str, content: str, deck_name: str, participants: list, manager):
    redis = await get_redis()
    safe_deck_name = deck_name.replace(" ", "_")
    redis_key = f"anki_session:{user}:{safe_deck_name}"

    # 1. FETCH STATE
    raw_data = await redis.get(redis_key)
    if not raw_data:
        return

    session_data = json.loads(raw_data)
    notes = session_data.get("notes", [])

    # --- STEP 1: GATHER CANDIDATES (The Pre-Filter) ---
    # We find ALL words present in the string, regardless of review status.
    # This matches your requirement to "not check is_reviewed" here.
    candidate_notes = []

    for note in notes:
        card_front = note.get("front", "").strip()

        # Simple substring check to save AI tokens
        # We only send words that actually appear in the text
        if card_front and card_front.lower() in content.lower():
            candidate_notes.append(note)

    # If no words from the deck are even in the text, stop here.
    if not candidate_notes:
        return

    # --- STEP 2: AI VALIDATION ---
    # Prepare list of words for the LLM
    target_words = [n['front'] for n in candidate_notes]

    print(f" Asking AI to validate: {target_words} in '{content}'")

    # Call the AI (Single Request)
    ai_result = await check_usage_with_siliconflow(content, target_words)

    valid_word_strings = set(ai_result.get("valid_words", []))
    print(f"Valid Words: {valid_word_strings}")
    feedback = ai_result.get("feedback", "Good practice!")

    # --- STEP 3: UPDATE STATE BASED ON AI ---
    state_modified = False
    newly_reviewed_ids = []

    for note in candidate_notes:
        # Check if this specific note was approved by AI
        if note['front'] in valid_word_strings:

            # Now we apply the logic to only tick it if not already done
            # (Or re-tick it if you want to allow practice reps)
            if not note.get("is_reviewed"):
                note["is_reviewed"] = True
                state_modified = True

            # We add it to the payload regardless, so the UI shows the "Sticky Note"
            # for this specific message
            newly_reviewed_ids.append({
                "id": note["id"],
                "word": note['front']
            })

    # 4. SAVE & BROADCAST
    if newly_reviewed_ids:  # Broadcast if ANY words were valid (even if already reviewed)

        # Only write to Redis if we actually changed an 'is_reviewed' status
        if state_modified:
            session_data["notes"] = notes
            await redis.set(redis_key, json.dumps(session_data), ex=86400)

        payload = {
            "type": "learning_update",
            "ticked_notes": newly_reviewed_ids,
            "message_id": message_id,
            "message_review": feedback,  # <--- The AI Feedback
            "deck_name": deck_name,
            "learner": user,
            "timestamp": str(datetime.now(timezone.utc))
        }

        await manager.broadcast_to_participants(
            participants=participants,
            message=payload,
            sender=user
        )