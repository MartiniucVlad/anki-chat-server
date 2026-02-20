from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional
import json
import asyncio
import spacy
from functools import partial

from database_clients.database_mongo import get_db
from database_clients.database_redis import get_redis
from security import get_current_user
from pymongo.database import Database as PyMongoDatabase

router = APIRouter(tags=["Stories"])

# ---------------------------------------------------------------------------
# spaCy model — loaded once at startup
# ---------------------------------------------------------------------------
try:
    nlp = spacy.load("de_core_news_sm")
except OSError:
    import spacy.cli

    spacy.cli.download("de_core_news_sm")
    nlp = spacy.load("de_core_news_sm")

# A basic German frequency list would replace this — this is a placeholder
# showing the architecture. Swap with a real ranked wordlist loaded from file.
COMMON_WORDS_TOP_2000: set[str] = set()  # populate from a txt file at startup


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class StoryUploadRequest(BaseModel):
    title: str
    content: str
    source_url: Optional[str] = None  # where it came from, if anywhere
    tags: list[str] = []


class StorySummary(BaseModel):
    id: str
    title: str
    difficulty_label: str  # computed, not user-supplied
    difficulty_score: float
    word_count: int
    unique_word_count: int
    created_at: str
    is_public: bool


# ---------------------------------------------------------------------------
# Difficulty scoring
# ---------------------------------------------------------------------------
def compute_difficulty(doc, vocabulary: list[dict]) -> tuple[str, float]:
    """
    Returns (label, score) e.g. ("B1", 0.52)
    Score is 0.0 (easiest) to 1.0 (hardest).

    Heuristics:
    - Average sentence length
    - Ratio of content words not in top-2000 frequency list
    - Average word length of content words

    These weights are rough — tune them as you get real user feedback.
    """
    sentences = list(doc.sents)
    if not sentences:
        return ("A1", 0.0)

    avg_sentence_length = sum(len(s) for s in sentences) / len(sentences)

    content_lemmas = [
        t.lemma_.lower() for t in doc
        if t.is_alpha and not t.is_stop
    ]
    if not content_lemmas:
        return ("A1", 0.0)

    rare_ratio = (
            sum(1 for l in content_lemmas if l not in COMMON_WORDS_TOP_2000)
            / len(content_lemmas)
    )
    avg_word_length = sum(len(l) for l in content_lemmas) / len(content_lemmas)

    # Weighted score — sentence length normalized to ~30 max, word length to ~12
    score = (
            0.4 * min(rare_ratio, 1.0) +
            0.35 * min(avg_sentence_length / 30, 1.0) +
            0.25 * min(avg_word_length / 12, 1.0)
    )

    if score < 0.2:
        label = "A1"
    elif score < 0.35:
        label = "A2"
    elif score < 0.5:
        label = "B1"
    elif score < 0.65:
        label = "B2"
    elif score < 0.8:
        label = "C1"
    else:
        label = "C2"

    return (label, round(score, 4))


# ---------------------------------------------------------------------------
# Core ingestion logic (runs in background)
# ---------------------------------------------------------------------------
async def _run_ingestion(story_id: ObjectId, content: str, db: PyMongoDatabase, current_user: str):
    """
    Runs after the upload endpoint has already returned.
    Populates vocabulary, sentences, difficulty on the story document.
    """
    try:
        # spaCy is CPU-bound — run in thread pool so we don't block the event loop
        loop = asyncio.get_event_loop()
        doc = await loop.run_in_executor(None, partial(nlp, content))

        # --- Vocabulary extraction ---
        word_to_lemma: dict[str, str] = {}
        unique_lemmas: set[str] = set()

        for token in doc:
            if token.is_alpha and not token.is_stop:
                word_to_lemma[token.text] = token.lemma_.lower()
                unique_lemmas.add(token.lemma_.lower())

        # Batch dictionary lookup
        lemma_to_dict: dict[str, dict] = {}
        async for word_doc in db.dictionary.find(
                {"word": {"$in": list(unique_lemmas)}},
                {"word": 1, "pos": 1, "gender": 1, "senses": 1}  # project only what you need
        ):
            lemma_to_dict[word_doc["word"]] = {
                "dict_id": str(word_doc["_id"]),
                "lemma": word_doc["word"],
                "pos": word_doc.get("pos", "unknown"),
                "gender": word_doc.get("gender"),
                "translation": (
                    word_doc.get("senses", [{}])[0]
                    .get("glosses", ["No translation"])[0]
                ),
            }

        # Store as array — indexable in MongoDB
        vocabulary: list[dict] = []
        for surface, lemma in word_to_lemma.items():
            if lemma in lemma_to_dict:
                entry = lemma_to_dict[lemma].copy()
                entry["surface"] = surface  # the actual word as it appeared
                vocabulary.append(entry)



        # --- Difficulty scoring ---
        difficulty_label, difficulty_score = compute_difficulty(doc, vocabulary)

        # --- Update the story document ---
        await db.stories.update_one(
            {"_id": story_id},
            {"$set": {
                "status": "ready",
                "vocabulary": vocabulary,
                "difficulty_label": difficulty_label,
                "difficulty_score": difficulty_score,
                "unique_word_count": len(unique_lemmas)
            }}
        )

        # Invalidate the story list cache for this user
        redis = await get_redis()
        await redis.delete(f"stories:list:{current_user}")

    except Exception as e:
        # Mark as failed so the frontend can show an error state
        await db.stories.update_one(
            {"_id": story_id},
            {"$set": {"status": "failed", "error": str(e)}}
        )
        raise


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/stories/upload", status_code=202)
async def upload_story(
        req: StoryUploadRequest,
        background_tasks: BackgroundTasks,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Accepts the story immediately (202), saves a stub, and kicks off
    ingestion in the background. Frontend should poll /stories/{id}/status
    or use a WebSocket to know when it's ready.
    """
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Story content cannot be empty.")
    if len(req.content) > 100_000:
        raise HTTPException(status_code=400, detail="Story too long (max 100k characters).")

    result = await db.stories.insert_one({
        "uploader": current_user,
        "title": req.title.strip(),
        "content": req.content,
        "source_url": req.source_url,
        "tags": req.tags,
        "status": "processing",  # frontend polls this
        "is_public": False,
        "created_at": datetime.now(timezone.utc),
        # these get filled in by the background task
        "vocabulary": [],
        "difficulty_label": None,
        "difficulty_score": None,
        "word_count": None,
        "sentence_count": None,
    })

    background_tasks.add_task(_run_ingestion, result.inserted_id, req.content, db, current_user)


    return {
        "story_id": str(result.inserted_id),
        "status": "processing",
        "message": "Story received. Processing in background.",
    }


@router.get("/stories/{story_id}/status")
async def get_story_status(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """Lightweight poll endpoint — returns just the processing status."""
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID.")

    story = await db.stories.find_one(
        {"_id": oid, "uploader": current_user},
        {"status": 1, "error": 1, "difficulty_label": 1}
    )
    if not story:
        raise HTTPException(status_code=404, detail="Story not found.")

    return {
        "story_id": story_id,
        "status": story["status"],
        "difficulty_label": story.get("difficulty_label"),
        "error": story.get("error"),
    }


@router.get("/stories", response_model=list[StorySummary])
async def list_stories(
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    redis = await get_redis()
    cache_key = f"stories:list:{current_user}"
    cached = await redis.get(cache_key)

    if cached:
        return json.loads(cached)

    cursor = db.stories.find(
        # show the user's own stories + any public ones
        {"$or": [{"uploader": current_user}, {"is_public": True}]},
        # never return full content or vocabulary in a list view — keep it lean
        {"content": 0, "vocabulary": 0}
    ).sort("created_at", -1)

    stories = []
    async for s in cursor:
        # skip stories still processing — they're not useful to list yet
        if s.get("status") != "ready":
            continue
        stories.append({
            "id": str(s["_id"]),
            "title": s.get("title", "Untitled"),
            "difficulty_label": s.get("difficulty_label", "Unknown"),
            "difficulty_score": s.get("difficulty_score", 0.0),
            "word_count": s.get("word_count", 0),
            "unique_word_count": s.get("unique_word_count", 0),
            "created_at": s["created_at"].isoformat(),
            "is_public": s.get("is_public", False),
        })

    await redis.set(cache_key, json.dumps(stories), ex=3600)
    return stories


@router.get("/stories/{story_id}")
async def get_story(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """Full story with vocabulary — used when actually opening a story to read."""
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID.")

    story = await db.stories.find_one({
        "_id": oid,
        "$or": [{"uploader": current_user}, {"is_public": True}]
    })
    if not story:
        raise HTTPException(status_code=404, detail="Story not found.")
    if story.get("status") != "ready":
        raise HTTPException(status_code=409, detail="Story is still processing.")

    story["id"] = str(story.pop("_id"))
    return story