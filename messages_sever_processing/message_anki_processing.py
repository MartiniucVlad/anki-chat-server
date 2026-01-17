import json
import re
import unicodedata
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

from database_clients.database_redis import get_redis
from database_clients.database_mongo import get_db
from messages_sever_processing.llmvalidation import check_usage_with_siliconflow
from bson import ObjectId

import simplemma
from simplemma import simple_tokenizer

# A practical list of languages we expect/simplemma supports in your app.
_SIMPLEMMA_LANGS = {"en", "de", "fr", "es", "it", "pt", "nl", "ru", "uk", "ro"}

# Normalization helper regex
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def normalize_text(text: str) -> str:
    """
    Standardizes text: NFD normalization (strips accents), lowercase, strip whitespace.
    Essential for robust matching (e.g. 'Über' -> 'uber').
    """
    if not text:
        return ""
    text = unicodedata.normalize('NFD', text)
    text = "".join(c for c in text if unicodedata.category(c) != 'Mn')
    return text.lower().strip()


def _lemmatize_token(token: str, lang_code: str) -> str:
    """Wrap simplemma with safe fallback."""
    token = token.lower()
    if not token:
        return token
    try:
        if lang_code in _SIMPLEMMA_LANGS:
            return simplemma.lemmatize(token, lang=lang_code, greedy=True).lower()
        else:
            # unknown lang: attempt english lemmatize to be conservative
            return simplemma.lemmatize(token, lang="en", greedy=True).lower()
    except Exception:
        # fallback to token itself
        return token


def precompute_notes(notes: List[Dict[str, Any]], default_lang: str) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Precompute/annotate notes with:
      - normalized_front (str)
      - front_tokens (List[str])
      - front_lemmas (List[str])
      - single_word_lemma (str) for O(1) lookup on single-word cards
    Returns (new_notes_list, changed_flag)
    """
    changed = False
    new_notes = []
    for note in notes:
        note_copy = dict(note)  # shallow copy to avoid surprises
        front = normalize_text(note_copy.get("front", ""))
        # Use the stored language, fallback to the deck default passed in
        note_lang = (note_copy.get("language") or default_lang or "en")[:2].lower()

        # only annotate if missing or if cached lang differs (e.g. user changed deck lang)
        needs = False
        if note_copy.get("_normalized_front") != front:
            needs = True
        if note_copy.get("_lang") != note_lang:
            needs = True
        if "_front_lemmas" not in note_copy:
            needs = True

        if needs:
            changed = True
            note_copy["_normalized_front"] = front
            note_copy["_lang"] = note_lang

            # tokens and lemmas
            tokens = list(simple_tokenizer(front))
            tokens = [t.lower() for t in tokens if t.strip()]
            lemmas = [_lemmatize_token(t, note_lang) for t in tokens]

            note_copy["_front_tokens"] = tokens
            note_copy["_front_lemmas"] = lemmas

            if len(lemmas) == 1:
                note_copy["_single_word_lemma"] = lemmas[0]
            else:
                note_copy["_single_word_lemma"] = None

        new_notes.append(note_copy)

    return new_notes, changed


def make_ngram_set(lemmas: List[str], max_n: int = 5) -> set:
    """
    Build a set of ngrams (joined by space) for quick membership testing.
    Limit n to avoid explosion. max_n should be >= longest phrase length you expect.
    """
    s = set()
    L = len(lemmas)
    for n in range(1, min(max_n, L) + 1):
        for i in range(0, L - n + 1):
            s.add(" ".join(lemmas[i:i + n]))
    return s


def find_note_matches(content: str, notes: List[Dict[str, Any]], deck_name: str, session_data: Dict[str, Any] = None) -> \
List[Dict[str, Any]]:
    """
    Efficient pre-filter using:
      - precomputed note lemmas stored in notes[_front_lemmas]
      - content-token + content-lemma set
      - single-word cards: O(1) set lookup
      - multi-word cards: lemma n-gram membership
    If session_data is provided and notes were mutated (precompute step), caller
    may save it back to Redis.
    """
    if not content or not notes:
        return []

    # 1. DETECT LANGUAGE (Retrieve from session data, detected during persistence)
    # Default to 'en' if missing, but preferably use the one stored in Redis
    lang_code = session_data.get("target_language", "en") if session_data else "en"

    # 2. ENSURE NOTES ARE PRECOMPUTED
    # We check for our internal flags. If missing, or if lang changed, we re-compute.
    needs_precompute = any(
        "_normalized_front" not in n or
        "_front_lemmas" not in n or
        "_lang" not in n
        for n in notes
    )

    if needs_precompute:
        # Pass the detected lang_code so lemmas are generated correctly (e.g. German vs English)
        notes, changed = precompute_notes(notes, default_lang=lang_code)

        # If caller passed session_data, update it so caller can persist to Redis
        if changed and session_data is not None:
            session_data["notes"] = notes

    # 3. COMPUTE CONTENT TOKENS & LEMMAS
    # Use the SAME language code for the chat content as the deck
    content_tokens = [t.lower() for t in simple_tokenizer(content)]
    content_lemmas = [_lemmatize_token(t, lang_code) for t in content_tokens]

    # build fast lookup structures
    token_set = set(content_tokens)
    lemma_set = set(content_lemmas)
    ngram_set = make_ngram_set(content_lemmas, max_n=6)  # tune max_n as needed

    candidate_notes = []
    for note in notes:
        front_norm = note.get("_normalized_front") or normalize_text(note.get("front", ""))

        # quick checks

        # a) Single-token cards: compare both token and lemma
        # (e.g. User types "cats", card is "cat". Lemma matches.)
        if note.get("_single_word_lemma"):
            if note["_single_word_lemma"] in lemma_set or front_norm in token_set:
                candidate_notes.append(note)
                continue

        # b) Short single tokens which might have been missed by lemmatizer
        if len(note.get("_front_tokens", [])) == 1:
            token = note["_front_tokens"][0]
            if token in token_set or token in lemma_set:
                candidate_notes.append(note)
                continue

        # c) Multi-word cards: check lemma n-gram membership
        # (e.g. Card "pomme de terre", User types "pommes de terre")
        front_lemmas = note.get("_front_lemmas") or []
        if front_lemmas:
            joined = " ".join(front_lemmas)
            if joined in ngram_set:
                candidate_notes.append(note)
                continue

        # d) Fallback substring match on normalized strings
        # (Last resort for punctuation/formatting edge cases)
        if " " in front_norm and front_norm in normalize_text(content):
            candidate_notes.append(note)
            continue

    return candidate_notes


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

    # --- STEP 1: GATHER CANDIDATES (The Smart Pre-Filter) ---
    # This now uses the stored language from session_data inside find_note_matches
    candidate_notes = find_note_matches(content, notes, deck_name, session_data=session_data)

    # If no words from the deck are detected linguistically, stop here.
    if not candidate_notes:
        return

    # --- STEP 2: AI VALIDATION ---
    # Prepare list of words for the LLM
    target_words = [n['front'] for n in candidate_notes]

    print(f"[{deck_name}] Asking AI to validate: {target_words} in '{content}'")

    # Call the AI (Single Request)
    ai_result = await check_usage_with_siliconflow(content, target_words)

    valid_word_strings = set(ai_result.get("valid_words", []))
    print(f"Valid Words Confirmed by AI: {valid_word_strings}")
    feedback = ai_result.get("feedback", "Good practice!")

    # --- STEP 3: UPDATE STATE BASED ON AI ---
    state_modified = False
    newly_reviewed_ids = []

    for note in candidate_notes:
        # Check if this specific note was approved by AI
        if note['front'] in valid_word_strings:

            # Logic: Tick if not already done
            if not note.get("is_reviewed"):
                note["is_reviewed"] = True
                state_modified = True

            # Add to payload for UI "Sticky Note"
            newly_reviewed_ids.append({
                "id": note["id"],
                "word": note['front']
            })

    # 4. SAVE & BROADCAST

    # Only write to Redis if we actually changed an 'is_reviewed' status
    if state_modified:
        session_data["notes"] = notes
        # Refresh TTL (e.g., 24 hours)
        await redis.set(redis_key, json.dumps(session_data), ex=86400)

    # Prepare payload for real-time update
    payload = {
        "type": "learning_update",
        "ticked_notes": newly_reviewed_ids,
        "message_id": message_id,
        "message_review": feedback,
        "deck_name": deck_name,
        "learner": user,
        "timestamp": str(datetime.now(timezone.utc))
    }

    await manager.broadcast_to_participants(
        participants=participants,
        message=payload,
        sender=user
    )

    # Persist to MongoDB for history
    db = get_db()

    review_data = {
        "ticked_notes": newly_reviewed_ids,
        "message_review": feedback,
        "deck_name": deck_name,
        "processed_at": datetime.now(timezone.utc)
    }

    await db.messages.update_one(
        {"_id": ObjectId(message_id)},
        {"$set": {"anki_review": review_data}}
    )