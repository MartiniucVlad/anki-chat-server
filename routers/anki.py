# backend/routers/chat.py

from fastapi import APIRouter
from websocket_manager import manager
from jose import jwt, JWTError
from security import SECRET_KEY, ALGORITHM, get_current_user
from database_clients.database_mongo import get_db
from models import AnkiNote, AnkiDeckNotes
from pymongo.database import Database as PyMongoDatabase
import json
from database_clients.database_redis import get_redis
import asyncio
from message_handling.search_service import *
from datetime import datetime, timezone
from fastapi import WebSocket, WebSocketDisconnect, Query, Depends



router = APIRouter(tags=["Anki"], prefix="/anki")


@router.post("/active-deck-persistence", response_model=AnkiDeckNotes)
async def stored_deck_notes(
        deck: AnkiDeckNotes,
        user: str = Depends(get_current_user)
):
    redis = await get_redis()
    safe_deck_name = deck.deck_name.replace(" ", "_")
    redis_key = f"anki_session:{user}:{safe_deck_name}"

    raw_data = await redis.get(redis_key)

    # 2. Create Progress Map
    # Key = Note ID
    # Value = { is_reviewed: bool, mod: int }
    progress_map = {}
    if raw_data:
        stored_session = json.loads(raw_data)
        for note in stored_session.get("notes", []):
            progress_map[note['id']] = {
                'is_reviewed': note.get('is_reviewed', False),
                'mod': note.get('mod', 0)
            }
        print(f"Found existing session for {deck.deck_name}.")

    # 3. Build Final List with STALE CHECK
    final_notes = []

    for note in deck.notes:
        note_dict = note.model_dump()

        # Default to False
        note_dict['is_reviewed'] = False

        if note.id in progress_map:
            stored_data = progress_map[note.id]
            # If incoming mod != stored mod, the card was changed/reviewed in Anki
            # since we last cached it, so we treat it as a NEW review.
            if note.mod == stored_data['mod']:
                note_dict['is_reviewed'] = stored_data['is_reviewed']
            else:
                print(f"Card {note.id} has changed (New Version). Resetting status.")

        final_notes.append(note_dict)

    # 4. Save
    session_data = {
        "deck_name": deck.deck_name,
        "notes": final_notes
    }
    await redis.set(redis_key, json.dumps(session_data), ex=86400)

    return AnkiDeckNotes(
        deck_name=deck.deck_name,
        notes=final_notes
    )

