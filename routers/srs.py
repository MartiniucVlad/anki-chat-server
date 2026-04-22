# backend/routers/srs.py

from __future__ import annotations

import spacy
from functools import partial
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
from bson import ObjectId

from fsrs import Scheduler, Card, Rating as FSRSRating, ReviewLog, State, Optimizer

from database_clients.database_mongo import get_db
from security import get_current_user
from pymongo.database import Database as PyMongoDatabase

from models.srs_models import (
    CardFront,
    CardBack,
    CreateCardRequest,
    BulkCreateCardsRequest,
    StoryVocabCardRequest,
    CreateDeckRequest,
    UpdateDeckRequest,
    SubmitReviewRequest,
    CardResponse,
    DeckResponse,
    ReviewSessionResponse,
    ReviewResultResponse,
    DeckStatsResponse,
    GlobalStatsResponse,
    BulkCreateResult,
    OptimizeResponse,
)

router = APIRouter(tags=["SRS"], prefix="/srs")

# ---------------------------------------------------------------------------
# spaCy — shared with the stories router; spaCy caches loaded models in-memory
# ---------------------------------------------------------------------------
try:
    _nlp = spacy.load("de_core_news_md")
except OSError:
    spacy.cli.download("de_core_news_md")
    _nlp = spacy.load("de_core_news_md")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_STATE_LABELS: dict[int, str] = {1: "Learning", 2: "Review", 3: "Relearning"}
_RATING_LABELS: dict[int, str] = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}
_DEFAULT_BATCH_SIZE = 20
_OPTIMIZER_MIN_REVIEWS = 20


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------
def _get_scheduler(user_config: dict | None) -> Scheduler:
    """Build an FSRS Scheduler from per-user config, falling back to defaults."""
    kwargs: dict = {}

    if user_config:
        if user_config.get("parameters"):
            kwargs["parameters"] = tuple(user_config["parameters"])
        kwargs["desired_retention"] = user_config.get("desired_retention", 0.9)
        kwargs["maximum_interval"] = user_config.get("maximum_interval", 36500)
        kwargs["enable_fuzzing"] = user_config.get("enable_fuzzing", True)

        lr = user_config.get("learning_steps") or []
        kwargs["learning_steps"] = tuple(timedelta(seconds=s) for s in lr)

        rl = user_config.get("relearning_steps") or []
        kwargs["relearning_steps"] = tuple(timedelta(seconds=s) for s in rl)

    return Scheduler(**kwargs)


async def _load_user_config(db: PyMongoDatabase, user_id: str) -> dict | None:
    return await db.srs_user_config.find_one({"user_id": user_id})


# ---------------------------------------------------------------------------
# Card ↔ FSRS serialisation helpers
# ---------------------------------------------------------------------------
def _new_fsrs_card() -> Card:
    """Create a fresh FSRS card (state=Learning, due=now)."""
    return Card()


def _fsrs_card_to_doc_fields(card: Card) -> dict:
    """Return the fields to $set on a card document after a review."""
    return {
        "fsrs_state": card.to_json(),
        "due": card.due,
        "state": card.state,
        "stability": card.stability,
        "difficulty": card.difficulty,
        "reps": card.reps,
        "lapses": card.lapses,
        "updated_at": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------
def _card_to_response(doc: dict, scheduler: Scheduler) -> CardResponse:
    fsrs_card = Card.from_json(doc["fsrs_state"])
    retrievability = scheduler.get_card_retrievability(fsrs_card)

    return CardResponse(
        card_id=str(doc["_id"]),
        deck_id=str(doc["deck_id"]),
        front=CardFront(**doc["front"]),
        back=CardBack(**doc["back"]),
        source_type=doc.get("source_type"),
        source_id=doc.get("source_id"),
        lemma=doc["lemma"],
        state=fsrs_card.state,
        state_label=_STATE_LABELS.get(fsrs_card.state, "Unknown"),
        due=fsrs_card.due,
        stability=fsrs_card.stability,
        difficulty=fsrs_card.difficulty,
        reps=fsrs_card.reps,
        lapses=fsrs_card.lapses,
        retrievability=round(retrievability, 4),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _deck_to_response(doc: dict, counts: dict) -> DeckResponse:
    return DeckResponse(
        deck_id=str(doc["_id"]),
        name=doc["name"],
        description=doc.get("description"),
        language=doc["language"],
        card_count=counts["total"],
        due_count=counts["due"],
        new_count=counts["new"],
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )


def _scheduling_info(prev: Card, new: Card, rating: int) -> dict:
    interval_s = (new.due - datetime.now(timezone.utc)).total_seconds()
    interval_d = max(0.0, interval_s / 86400)

    msgs: list[str] = []
    if prev.state == State.Learning and new.state == State.Review:
        msgs.append("Card graduated!")
    elif prev.state == State.Review and new.state == State.Relearning:
        msgs.append("Card lapsed.")
    elif prev.state == State.Relearning and new.state == State.Review:
        msgs.append("Card re-graduated.")

    if interval_s < 60:
        msgs.append(f"Next in {int(interval_s)}s.")
    elif interval_s < 3600:
        msgs.append(f"Next in {int(interval_s / 60)}m.")
    elif interval_d < 1:
        msgs.append(f"Next in {int(interval_s / 3600)}h.")
    else:
        msgs.append(f"Next in {interval_d:.1f}d.")

    return {
        "rating_label": _RATING_LABELS.get(rating, "?"),
        "previous_state": _STATE_LABELS.get(prev.state, "Unknown"),
        "new_state": _STATE_LABELS.get(new.state, "Unknown"),
        "new_due": new.due.isoformat(),
        "interval_days": round(interval_d, 2),
        "message": " ".join(msgs),
    }


# ---------------------------------------------------------------------------
# Context-sentence extraction (for story-vocab cards)
# ---------------------------------------------------------------------------
def _extract_context(chunk_text: str, surfaces: list[str]) -> tuple[str | None, str | None]:
    """
    Run spaCy on the chunk, find the first sentence containing any surface
    form of the target word, and return (blanked_context, full_example).
    """
    doc = _nlp(chunk_text)
    surfaces_lower = {s.lower() for s in surfaces}

    for sent in doc.sents:
        sent_text = sent.text.strip()
        for token in sent:
            if token.text.lower() in surfaces_lower or token.lemma_.lower() in {s.lower() for s in surfaces}:
                example = sent_text
                # Replace only the first occurrence of the exact surface
                context = sent_text.replace(token.text, "___", 1)
                return context, example

    return None, None


# ---------------------------------------------------------------------------
# Internal bulk-insert (shared by endpoint + chat-integration helper)
# ---------------------------------------------------------------------------
async def _bulk_insert_cards(
    db: PyMongoDatabase,
    user_id: str,
    deck_id_str: str,
    cards: list[CreateCardRequest],
    skip_duplicates: bool = True,
) -> BulkCreateResult:
    deck_oid = ObjectId(deck_id_str)
    now = datetime.now(timezone.utc)
    fsrs = _new_fsrs_card()

    created_ids: list[str] = []
    skipped = 0

    for req in cards:
        lemma = (req.lemma or req.front.word).lower()

        if skip_duplicates:
            exists = await db.srs_cards.find_one(
                {"user_id": user_id, "deck_id": deck_oid, "lemma": lemma},
                projection={"_id": 1},
            )
            if exists:
                skipped += 1
                continue

        doc = {
            "user_id": user_id,
            "deck_id": deck_oid,
            "front": req.front.model_dump(),
            "back": req.back.model_dump(),
            "source_type": req.source_type,
            "source_id": req.source_id,
            "lemma": lemma,
            **_fsrs_card_to_doc_fields(fsrs),
            "created_at": now,
        }

        result = await db.srs_cards.insert_one(doc)
        created_ids.append(str(result.inserted_id))

    return BulkCreateResult(
        created=len(created_ids),
        skipped_duplicates=skipped,
        card_ids=created_ids,
    )


# ---------------------------------------------------------------------------
# Deck aggregate counts
# ---------------------------------------------------------------------------
async def _deck_counts(db: PyMongoDatabase, user_id: str, deck_oid: ObjectId) -> dict:
    pipeline = [
        {"$match": {"user_id": user_id, "deck_id": deck_oid}},
        {"$facet": {
            "total":  [{"$count": "n"}],
            "due":    [{"$match": {"due": {"$lte": datetime.now(timezone.utc)}}},
                       {"$count": "n"}],
            "new":    [{"$match": {"state": State.Learning, "reps": 0}},
                       {"$count": "n"}],
        }},
    ]
    row = await db.srs_cards.aggregate(pipeline).to_list(1)
    f = row[0] if row else {}
    return {
        "total": f["total"][0]["n"] if f.get("total") else 0,
        "due":   f["due"][0]["n"]   if f.get("due")   else 0,
        "new":   f["new"][0]["n"]   if f.get("new")   else 0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  DECK  CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/decks", response_model=DeckResponse, status_code=201)
async def create_deck(
    body: CreateDeckRequest,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    doc = {
        "user_id": user,
        "name": body.name,
        "description": body.description,
        "language": body.language,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.srs_decks.insert_one(doc)
    counts = {"total": 0, "due": 0, "new": 0}
    return _deck_to_response({**doc, "_id": result.inserted_id}, counts)


@router.get("/decks", response_model=list[DeckResponse])
async def list_decks(
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    cursor = db.srs_decks.find({"user_id": user}).sort("updated_at", -1)
    out: list[DeckResponse] = []
    async for doc in cursor:
        counts = await _deck_counts(db, user, doc["_id"])
        out.append(_deck_to_response(doc, counts))
    return out


@router.get("/decks/{deck_id}", response_model=DeckResponse)
async def get_deck(
    deck_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    doc = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not doc:
        raise HTTPException(404, "Deck not found")
    counts = await _deck_counts(db, user, doc["_id"])
    return _deck_to_response(doc, counts)


@router.patch("/decks/{deck_id}", response_model=DeckResponse)
async def update_deck(
    deck_id: str,
    body: UpdateDeckRequest,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    doc = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not doc:
        raise HTTPException(404, "Deck not found")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return doc

    updates["updated_at"] = datetime.now(timezone.utc)
    await db.srs_decks.update_one({"_id": ObjectId(deck_id)}, {"$set": updates})

    updated = await db.srs_decks.find_one({"_id": ObjectId(deck_id)})
    counts = await _deck_counts(db, user, updated["_id"])
    return _deck_to_response(updated, counts)


@router.delete("/decks/{deck_id}")
async def delete_deck(
    deck_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    doc = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not doc:
        raise HTTPException(404, "Deck not found")

    deck_oid = ObjectId(deck_id)

    # Gather card IDs before deleting cards (needed for review-log cleanup)
    card_oids = [
        c["_id"] async for c in db.srs_cards.find(
            {"deck_id": deck_oid}, projection={"_id": 1}
        )
    ]

    if card_oids:
        await db.srs_cards.delete_many({"deck_id": deck_oid})
        await db.srs_review_logs.delete_many({"card_id": {"$in": card_oids}})

    await db.srs_decks.delete_one({"_id": deck_oid})
    return {"status": "deleted", "cards_removed": len(card_oids)}


# ═══════════════════════════════════════════════════════════════════════════════
#  CARD  CRUD
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/decks/{deck_id}/cards", response_model=BulkCreateResult, status_code=201)
async def bulk_create_cards(
    deck_id: str,
    body: BulkCreateCardsRequest,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    # Verify deck exists and belongs to user
    deck = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not deck:
        raise HTTPException(404, "Deck not found")

    return await _bulk_insert_cards(db, user, deck_id, body.cards, body.skip_duplicates)


@router.get("/decks/{deck_id}/cards", response_model=list[CardResponse])
async def list_cards(
    deck_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
    state: Optional[int] = Query(None, ge=1, le=3, description="Filter by FSRS state"),
    sort: str = Query("created", regex="^(created|due|difficulty|reps)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    deck = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not deck:
        raise HTTPException(404, "Deck not found")

    match_filter: dict = {"user_id": user, "deck_id": ObjectId(deck_id)}
    if state is not None:
        match_filter["state"] = state

    sort_map = {
        "created":   ("created_at", -1),
        "due":       ("due", 1),
        "difficulty": ("difficulty", -1),
        "reps":      ("reps", -1),
    }
    sort_key, sort_dir = sort_map.get(sort, ("created_at", -1))

    user_config = await _load_user_config(db, user)
    scheduler = _get_scheduler(user_config)

    cursor = db.srs_cards.find(match_filter).sort(sort_key, sort_dir).skip(offset).limit(limit)
    return [_card_to_response(doc, scheduler) async for doc in cursor]


@router.delete("/cards/{card_id}")
async def delete_card(
    card_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    result = await db.srs_cards.delete_one({"_id": ObjectId(card_id), "user_id": user})
    if result.deleted_count == 0:
        raise HTTPException(404, "Card not found")
    # Clean up review logs
    await db.srs_review_logs.delete_many({"card_id": ObjectId(card_id)})
    return {"status": "deleted"}


# ═══════════════════════════════════════════════════════════════════════════════
#  CREATE CARD FROM STORY VOCABULARY
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/decks/{deck_id}/cards/from-story-vocab", response_model=CardResponse, status_code=201)
async def create_card_from_story_vocab(
    deck_id: str,
    body: StoryVocabCardRequest,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    """
    One-click card creation from a story chunk's pre-extracted vocabulary.
    The backend looks up the chunk, finds the vocab entry, extracts a
    context sentence via spaCy, and builds a fully annotated card.
    """
    # Verify deck
    deck = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not deck:
        raise HTTPException(404, "Deck not found")

    # Find chunk
    chunk = await db.story_chunks.find_one({
        "story_id": ObjectId(body.story_id),
        "chunk_index": body.chunk_index,
    })
    if not chunk:
        raise HTTPException(404, "Story chunk not found")

    # Find vocab entry by lemma (case-insensitive)
    vocab_entry = None
    for v in chunk.get("vocabulary", []):
        if v["lemma"].lower() == body.lemma.lower():
            vocab_entry = v
            break

    if not vocab_entry:
        raise HTTPException(404, f"Lemma '{body.lemma}' not found in chunk vocabulary")

    # Extract context sentence
    surfaces = vocab_entry.get("surfaces", [vocab_entry["lemma"]])
    context, example = _extract_context(chunk["content"], surfaces)

    # Build card
    req = CreateCardRequest(
        front=CardFront(
            word=vocab_entry["lemma"],
            context_sentence=context,
            part_of_speech=vocab_entry.get("pos"),
        ),
        back=CardBack(
            definition="; ".join(vocab_entry.get("definitions", [])),
            gender=vocab_entry.get("gender"),
            plural=(vocab_entry.get("plurals") or [None])[0] if vocab_entry.get("plurals") else None,
            example_sentence=example,
            synonyms=vocab_entry.get("synonyms", []),
            form_info=vocab_entry.get("form_info"),
        ),
        source_type=body.source_type,
        source_id=body.story_id,
        lemma=vocab_entry["lemma"].lower(),
    )

    result = await _bulk_insert_cards(db, user, deck_id, [req], skip_duplicates=True)
    if result.created == 0:
        raise HTTPException(409, f"Card for '{body.lemma}' already exists in this deck")

    # Fetch the created card for the response
    user_config = await _load_user_config(db, user)
    scheduler = _get_scheduler(user_config)
    card_doc = await db.srs_cards.find_one({"_id": ObjectId(result.card_ids[0])})
    return _card_to_response(card_doc, scheduler)


# ═══════════════════════════════════════════════════════════════════════════════
#  REVIEW  SESSION
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/decks/{deck_id}/review", response_model=ReviewSessionResponse)
async def get_review_session(
    deck_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
    batch_size: int = Query(_DEFAULT_BATCH_SIZE, ge=1, le=100),
):
    """
    Returns a batch of due cards for a review session, sorted by most-overdue
    first. The frontend cycles through them and submits ratings one by one.
    """
    deck = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not deck:
        raise HTTPException(404, "Deck not found")

    now = datetime.now(timezone.utc)
    total_due = await db.srs_cards.count_documents({
        "user_id": user,
        "deck_id": ObjectId(deck_id),
        "due": {"$lte": now},
    })

    if total_due == 0:
        return ReviewSessionResponse(deck_id=deck_id, cards=[], total_due=0, returned=0)

    user_config = await _load_user_config(db, user)
    scheduler = _get_scheduler(user_config)

    cursor = (
        db.srs_cards.find(
            {"user_id": user, "deck_id": ObjectId(deck_id), "due": {"$lte": now}},
        )
        .sort("due", 1)
        .limit(batch_size)
    )

    cards = [_card_to_response(doc, scheduler) async for doc in cursor]
    return ReviewSessionResponse(
        deck_id=deck_id,
        cards=cards,
        total_due=total_due,
        returned=len(cards),
    )


@router.post("/cards/{card_id}/review", response_model=ReviewResultResponse)
async def submit_review(
    card_id: str,
    body: SubmitReviewRequest,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    """
    Submit a review rating for a card. The FSRS algorithm updates the card's
    scheduling state and a ReviewLog is persisted for future optimisation.
    """
    card_doc = await db.srs_cards.find_one({"_id": ObjectId(card_id), "user_id": user})
    if not card_doc:
        raise HTTPException(404, "Card not found")

    user_config = await _load_user_config(db, user)
    scheduler = _get_scheduler(user_config)

    # Deserialize previous state
    prev_fsrs = Card.from_json(card_doc["fsrs_state"])

    # Build optional review_duration
    review_duration = (
        timedelta(milliseconds=body.review_duration_ms)
        if body.review_duration_ms
        else None
    )

    # Run FSRS review
    rating = FSRSRating(body.rating)
    new_fsrs, review_log = scheduler.review_card(prev_fsrs, rating, review_duration)

    # Persist updated card
    update_fields = _fsrs_card_to_doc_fields(new_fsrs)
    await db.srs_cards.update_one(
        {"_id": ObjectId(card_id)},
        {"$set": update_fields},
    )

    # Persist review log
    log_doc = {
        "user_id": user,
        "card_id": ObjectId(card_id),
        "deck_id": card_doc["deck_id"],
        "rating": body.rating,
        "review_datetime": review_log.review_datetime,
        "review_duration_ms": body.review_duration_ms,
        "fsrs_log": review_log.to_json(),
    }
    await db.srs_review_logs.insert_one(log_doc)

    # Build response
    updated_doc = await db.srs_cards.find_one({"_id": ObjectId(card_id)})
    card_response = _card_to_response(updated_doc, scheduler)
    scheduling = _scheduling_info(prev_fsrs, new_fsrs, body.rating)

    return ReviewResultResponse(card=card_response, scheduling=scheduling)


@router.post("/cards/{card_id}/reset", response_model=CardResponse)
async def reset_card(
    card_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    """Reset a card to Learning state (fresh, due immediately)."""
    card_doc = await db.srs_cards.find_one({"_id": ObjectId(card_id), "user_id": user})
    if not card_doc:
        raise HTTPException(404, "Card not found")

    fresh = _new_fsrs_card()
    update_fields = _fsrs_card_to_doc_fields(fresh)
    await db.srs_cards.update_one({"_id": ObjectId(card_id)}, {"$set": update_fields})

    user_config = await _load_user_config(db, user)
    scheduler = _get_scheduler(user_config)
    updated_doc = await db.srs_cards.find_one({"_id": ObjectId(card_id)})
    return _card_to_response(updated_doc, scheduler)


# ═══════════════════════════════════════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/decks/{deck_id}/stats", response_model=DeckStatsResponse)
async def deck_stats(
    deck_id: str,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    deck = await db.srs_decks.find_one({"_id": ObjectId(deck_id), "user_id": user})
    if not deck:
        raise HTTPException(404, "Deck not found")

    deck_oid = ObjectId(deck_id)
    now = datetime.now(timezone.utc)

    pipeline = [
        {"$match": {"user_id": user, "deck_id": deck_oid}},
        {"$facet": {
            "total":    [{"$count": "n"}],
            "due":      [{"$match": {"due": {"$lte": now}}}, {"$count": "n"}],
            "learning": [{"$match": {"state": State.Learning}}, {"$count": "n"}],
            "review":   [{"$match": {"state": State.Review}},   {"$count": "n"}],
            "relearn":  [{"$match": {"state": State.Relearning}}, {"$count": "n"}],
            "mature":   [{"$match": {"reps": {"$gte": 3}}}, {"$count": "n"}],
            "agg":      [{"$group": {
                "_id": None,
                "avg_diff":  {"$avg": "$difficulty"},
                "avg_stab":  {"$avg": "$stability"},
            }}],
        }},
    ]
    row = await db.srs_cards.aggregate(pipeline).to_list(1)
    f = row[0] if row else {}

    total_reviews = await db.srs_review_logs.count_documents({
        "user_id": user, "deck_id": deck_oid,
    })

    agg = f["agg"][0] if f.get("agg") else {}

    return DeckStatsResponse(
        deck_id=deck_id,
        deck_name=deck["name"],
        total_cards=f["total"][0]["n"] if f.get("total") else 0,
        due_now=f["due"][0]["n"] if f.get("due") else 0,
        new_cards=f["learning"][0]["n"] if f.get("learning") else 0,
        learning_cards=f["learning"][0]["n"] if f.get("learning") else 0,
        review_cards=f["review"][0]["n"] if f.get("review") else 0,
        relearning_cards=f["relearn"][0]["n"] if f.get("relearn") else 0,
        average_difficulty=round(agg.get("avg_diff", 0.0), 2),
        average_stability=round(agg.get("avg_stab", 0.0), 2),
        total_reviews=total_reviews,
        mature_cards=f["mature"][0]["n"] if f.get("mature") else 0,
    )


@router.get("/stats", response_model=GlobalStatsResponse)
async def global_stats(
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return GlobalStatsResponse(
        total_decks=await db.srs_decks.count_documents({"user_id": user}),
        total_cards=await db.srs_cards.count_documents({"user_id": user}),
        total_due=await db.srs_cards.count_documents({"user_id": user, "due": {"$lte": now}}),
        total_reviews=await db.srs_review_logs.count_documents({"user_id": user}),
        cards_added_today=await db.srs_cards.count_documents({
            "user_id": user, "created_at": {"$gte": today_start},
        }),
        reviews_today=await db.srs_review_logs.count_documents({
            "user_id": user, "review_datetime": {"$gte": today_start},
        }),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEDULER OPTIMISATION (background task)
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_optimization(user_id: str, db: PyMongoDatabase):
    """
    Fetch all review logs for a user, run the FSRS optimizer to compute
    optimal parameters + retention, persist the config, and reschedule
    every card that has at least one review log.
    """
    import asyncio
    loop = asyncio.get_event_loop()

    # ── 1. Collect logs (single pass, grouped by card) ────────────────────
    all_logs: list[ReviewLog] = []
    card_logs: dict[str, list[ReviewLog]] = {}

    async for log_doc in db.srs_review_logs.find({"user_id": user_id}):
        rl = ReviewLog.from_json(log_doc["fsrs_log"])
        all_logs.append(rl)
        cid = str(log_doc["card_id"])
        card_logs.setdefault(cid, []).append(rl)

    if len(all_logs) < _OPTIMIZER_MIN_REVIEWS:
        return  # not enough data

    # ── 2. Optimise (CPU-bound → thread pool) ────────────────────────────
    def _optimize(logs: list[ReviewLog]) -> tuple:
        opt = Optimizer(logs)
        params = opt.compute_optimal_parameters()
        retention = opt.compute_optimal_retention(params)
        return params, retention

    optimal_params, optimal_retention = await loop.run_in_executor(
        None, _optimize, all_logs
    )

    # ── 3. Persist config ─────────────────────────────────────────────────
    await db.srs_user_config.update_one(
        {"user_id": user_id},
        {"$set": {
            "parameters": list(optimal_params),
            "desired_retention": optimal_retention,
            "last_optimized": datetime.now(timezone.utc),
        }},
        upsert=True,
    )

    # ── 4. Reschedule every card that has logs ────────────────────────────
    optimal_scheduler = Scheduler(optimal_params, optimal_retention)

    for cid, clogs in card_logs.items():
        card_doc = await db.srs_cards.find_one({
            "_id": ObjectId(cid), "user_id": user_id,
        })
        if not card_doc:
            continue

        fsrs_card = Card.from_json(card_doc["fsrs_state"])
        rescheduled = optimal_scheduler.reschedule_card(fsrs_card, clogs)

        await db.srs_cards.update_one(
            {"_id": ObjectId(cid)},
            {"$set": _fsrs_card_to_doc_fields(rescheduled)},
        )


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_scheduler(
    background_tasks: BackgroundTasks,
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    """
    Trigger FSRS parameter optimisation in the background. The optimizer
    analyses all of the user's past review logs to find the best scheduler
    parameters and retention target for that individual learner, then
    reschedules every card accordingly.

    Requires at least 20 review logs to produce meaningful results.
    """
    log_count = await db.srs_review_logs.count_documents({"user_id": user})

    if log_count < _OPTIMIZER_MIN_REVIEWS:
        return OptimizeResponse(
            status="skipped",
            message=f"Need at least {_OPTIMIZER_MIN_REVIEWS} reviews "
                    f"(you have {log_count}). Keep studying!",
        )

    background_tasks.add_task(_run_optimization, user, db)
    return OptimizeResponse(
        status="started",
        message=f"Optimising over {log_count} review logs. "
                f"This runs in the background — your cards will be "
                f"automatically rescheduled when it finishes.",
    )


@router.get("/optimize/status")
async def optimization_status(
    user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db),
):
    """Check when the scheduler was last optimised for this user."""
    config = await db.srs_user_config.find_one(
        {"user_id": user},
        {"last_optimized": 1, "desired_retention": 1, "parameters": 1},
    )
    if not config:
        return {"optimized": False, "last_optimized": None}

    return {
        "optimized": True,
        "last_optimized": config.get("last_optimized"),
        "desired_retention": config.get("desired_retention"),
        "uses_custom_params": config.get("parameters") is not None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  CHAT INTEGRATION HELPER
# ═══════════════════════════════════════════════════════════════════════════════
#
# Called from the chat router when the AI judge identifies mistakes.
# Not exposed as an HTTP endpoint — imported and used internally.
#

async def add_cards_from_chat_mistakes(
    db: PyMongoDatabase,
    user_id: str,
    deck_id: str,
    mistakes: list[dict],
) -> BulkCreateResult:
    """
    Auto-create SRS cards from AI-judge mistakes.

    Each mistake dict should contain:
        word: str          — the target lemma
        incorrect: str     — the learner's incorrect usage
        correct: str       — the corrected version
        explanation: str   — why it was wrong
        part_of_speech: str (optional)
        message_id: str    (optional, for source tracking)
    """
    cards: list[CreateCardRequest] = []

    for m in mistakes:
        word = m.get("word", "")
        correct = m.get("correct", "")

        # Blank the target word in the correct sentence for the front
        context = None
        if correct and word:
            # Replace first case-insensitive occurrence
            import re
            context = re.sub(re.escape(word), "___", correct, count=1, flags=re.IGNORECASE)
            if context == correct:
                context = None  # word wasn't found in sentence, skip context

        cards.append(CreateCardRequest(
            front=CardFront(
                word=word,
                context_sentence=context,
                part_of_speech=m.get("part_of_speech"),
            ),
            back=CardBack(
                definition=m.get("explanation", ""),
                example_sentence=correct,
            ),
            source_type="chat",
            source_id=m.get("message_id"),
            lemma=word.lower(),
        ))

    return await _bulk_insert_cards(db, user_id, deck_id, cards, skip_duplicates=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MONGODB INDEXES  (call once at app startup)
# ═══════════════════════════════════════════════════════════════════════════════

async def ensure_srs_indexes(db: PyMongoDatabase):
    await db.srs_decks.create_index("user_id")
    await db.srs_decks.create_index([("user_id", 1), ("updated_at", -1)])

    await db.srs_cards.create_index([("user_id", 1), ("deck_id", 1), ("lemma", 1)])
    await db.srs_cards.create_index([("user_id", 1), ("deck_id", 1), ("due", 1)])
    await db.srs_cards.create_index([("user_id", 1), ("deck_id", 1), ("state", 1)])

    await db.srs_review_logs.create_index(
        [("user_id", 1), ("card_id", 1), ("review_datetime", -1)]
    )
    await db.srs_review_logs.create_index(
        [("user_id", 1), ("review_datetime", -1)]
    )

    await db.srs_user_config.create_index("user_id", unique=True)