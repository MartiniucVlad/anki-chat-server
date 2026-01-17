# backend/routers/chat.py

from fastapi import APIRouter, Depends, HTTPException
from security import get_current_user
from models import AnkiDeckNotes, UpdateLangSchema
import json
from database_clients.database_redis import get_redis
from messages_sever_processing.anki_utils import detect_deck_language


router = APIRouter(tags=["Anki"], prefix="/anki")


@router.post("/active-deck-persistence", response_model=AnkiDeckNotes)
async def stored_deck_notes(
        deck: AnkiDeckNotes,
        user: str = Depends(get_current_user)
):
    # --- DEBUG PRINTS START ---
    print(f"\n[DEBUG] --- Received Deck Persistence Request ---")
    print(f"[DEBUG] User: {user}")
    print(f"[DEBUG] Deck Name: '{deck.deck_name}'")
    print(f"[DEBUG] Incoming Notes Count: {len(deck.notes)}")

    if len(deck.notes) > 0:
        print(f"[DEBUG] Sample Note [0]: {deck.notes[0]}")
    else:
        print(f"[DEBUG] WARNING: Received deck with 0 notes!")
    # --- DEBUG PRINTS END ---

    redis = await get_redis()
    safe_deck_name = deck.deck_name.replace(" ", "_")
    redis_key = f"anki_session:{user}:{safe_deck_name}"

    raw_data = await redis.get(redis_key)

    # Placeholders for existing state
    progress_map = {}
    existing_language = None

    # 2. Load Existing Session Data
    if raw_data:
        stored_session = json.loads(raw_data)
        # Capture the existing language if the user already set it manually
        existing_language = stored_session.get("target_language")

        for note in stored_session.get("notes", []):
            progress_map[note['id']] = {
                'is_reviewed': note.get('is_reviewed', False),
                'mod': note.get('mod', 0)
            }
        print(f"[DEBUG] Found existing session for {deck.deck_name} with {len(progress_map)} tracked notes.")

    # 3. Build Final List with STALE CHECK
    final_notes = []

    for note in deck.notes:
        note_dict = note.model_dump()

        # Default to False
        note_dict['is_reviewed'] = False

        if note.id in progress_map:
            stored_data = progress_map[note.id]
            # If incoming mod == stored mod, keep the review status
            if note.mod == stored_data['mod']:
                note_dict['is_reviewed'] = stored_data['is_reviewed']
            else:
                print(f"[DEBUG] Card {note.id} has changed (New Version). Resetting status.")

        final_notes.append(note_dict)

    print(f"[DEBUG] Final processed notes count: {len(final_notes)}")

    # 4. Handle Language Detection
    if existing_language:
        final_language = existing_language
        print(f"[DEBUG] Using existing language setting: {final_language}")
    else:
        # Convert Pydantic notes back to dicts if needed, or pass raw dicts
        final_language = detect_deck_language(final_notes)
        print(f"[DEBUG] Auto-detected language: {final_language}")

    # 5. Save to Redis
    session_data = {
        "deck_name": deck.deck_name,
        "notes": final_notes,
        "target_language": final_language
    }
    await redis.set(redis_key, json.dumps(session_data), ex=86400)

    # 6. Return to Frontend
    return AnkiDeckNotes(
        deck_name=deck.deck_name,
        notes=final_notes,
        language=final_language
    )

@router.post("/update-deck-language")
async def update_deck_language(
        payload: UpdateLangSchema,
        user: str = Depends(get_current_user)
):
    redis = await get_redis()
    safe_deck_name = payload.deck_name.replace(" ", "_")

    # Construct the key exactly as used in persistence
    redis_key = f"anki_session:{user}:{safe_deck_name}"

    raw_data = await redis.get(redis_key)

    if not raw_data:
        raise HTTPException(status_code=404, detail="Active deck session not found")

    session_data = json.loads(raw_data)

    # 1. Update the language setting
    session_data["target_language"] = payload.language


    # 2. Save back to Redis (Reset TTL to 24h)
    await redis.set(redis_key, json.dumps(session_data), ex=86400)

    return {
        "status": "success",
        "language": payload.language,
        "message": f"Language updated to {payload.language}. Cache cleared."
    }