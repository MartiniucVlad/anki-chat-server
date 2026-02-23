from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, File, Form, UploadFile
from pydantic import BaseModel
from bson import ObjectId
from datetime import datetime, timezone
from typing import Optional
import json
import asyncio
import spacy
from functools import partial
from spacy.tokens import Span


from database_clients.database_mongo import get_db
from database_clients.database_redis import get_redis
from security import get_current_user
from pymongo.database import Database as PyMongoDatabase

router = APIRouter(tags=["Stories"], prefix="/stories")

# ---------------------------------------------------------------------------
# spaCy model — loaded once at startup
# ---------------------------------------------------------------------------
try:
    nlp = spacy.load("de_core_news_md")
except OSError:
    import spacy.cli

    spacy.cli.download("de_core_news_md")
    nlp = spacy.load("de_core_news_md")

# Populate this set at startup by loading a German frequency wordlist from disk.
# Any plain text file with one lemma per line works (e.g. from hermit dave's frequency lists).
# Words in this set are considered "common" and lower the difficulty score.
COMMON_WORDS_TOP_2000: set[str] = set()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class StorySummary(BaseModel):
    id: str
    title: str
    difficulty_label: str
    difficulty_score: float
    word_count: int
    unique_word_count: int
    chunk_count: int
    created_at: str
    is_public: bool
    tags: list[str]


class StoryChunk(BaseModel):
    chunk_index: int
    content: str
    vocabulary: list[dict]
    word_count: int


class StoryDetail(BaseModel):
    id: str
    title: str
    difficulty_label: str
    difficulty_score: float
    word_count: int
    unique_word_count: int
    chunk_count: int
    source_url: Optional[str]
    tags: list[str]
    is_public: bool
    created_at: str
    chunks: list[StoryChunk]


# ---------------------------------------------------------------------------
# Difficulty scoring
# ---------------------------------------------------------------------------
def _compute_difficulty(doc) -> tuple[str, float]:
    """
    Returns (cefr_label, score) where score is 0.0 (easiest) → 1.0 (hardest).

    Three heuristics, each normalized to [0, 1] and weighted:
      - Rare word ratio: proportion of content lemmas not in the top-2000 list
      - Avg sentence length: normalized against a ~30-token ceiling
      - Avg content word length: normalized against a ~12-char ceiling

    These weights are intentionally simple — improve them once you have
    real user feedback or a labelled CEFR dataset to calibrate against.
    """
    sentences = list(doc.sents)
    if not sentences:
        return "A1", 0.0

    content_lemmas = [
        t.lemma_.lower()
        for t in doc
        if t.is_alpha and not t.is_stop
    ]
    if not content_lemmas:
        return "A1", 0.0

    rare_ratio = sum(
        1 for l in content_lemmas if l not in COMMON_WORDS_TOP_2000
    ) / len(content_lemmas)

    avg_sent_len = sum(len(s) for s in sentences) / len(sentences)
    avg_word_len = sum(len(l) for l in content_lemmas) / len(content_lemmas)

    score = (
            0.40 * min(rare_ratio, 1.0) +
            0.35 * min(avg_sent_len / 30, 1.0) +
            0.25 * min(avg_word_len / 12, 1.0)
    )
    score = round(score, 4)

    if score < 0.20:
        label = "A1"
    elif score < 0.35:
        label = "A2"
    elif score < 0.50:
        label = "B1"
    elif score < 0.65:
        label = "B2"
    elif score < 0.80:
        label = "C1"
    else:
        label = "C2"

    return label, score

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
WORDS_PER_CHUNK = 300  # target chunk size — adjust based on your UI


def _split_into_chunks(doc, words_per_chunk: int = WORDS_PER_CHUNK) -> list[str]:
    """
    Splits a spaCy doc into chunks of approximately `words_per_chunk` words.

    Key design decisions:
    - Uses `sent.text_with_ws` to reconstruct text — this preserves the
      author's original whitespace exactly, including paragraph breaks (\n\n),
      without any manual character-offset tracking.
    - Breaks at sentence boundaries only — never mid-sentence.
    - Flushes a chunk early if the *next* sentence starts after a paragraph
      break (detected via leading whitespace in `text_with_ws`) AND the
      current chunk is at least 75% full. This respects the author's intended
      structure when possible.
    - A sentence that is itself longer than `words_per_chunk` (e.g. a run-on)
      is placed in its own chunk rather than merged — it will never be skipped.

    Args:
        doc: A spaCy Doc object of the full story text.
        words_per_chunk: Soft target word count per chunk.

    Returns:
        Returns spaCy Span objects instead of strings.
    """
    sentences = list(doc.sents)
    chunks: list[Span] = []

    current_start: int = sentences[0].start if sentences else 0
    current_pieces: list[str] = []
    current_word_count: int = 0

    for i, sent in enumerate(sentences):
        sent_text_with_ws = sent.text_with_ws
        sent_word_count = len(sent)
        next_is_new_paragraph = "\n\n" in sent_text_with_ws or "\r\n\r\n" in sent_text_with_ws

        current_word_count += sent_word_count
        current_pieces.append(sent_text_with_ws)

        at_target = current_word_count >= words_per_chunk
        near_target_at_paragraph = (
                next_is_new_paragraph and current_word_count >= words_per_chunk * 0.75
        )

        should_flush = at_target or near_target_at_paragraph

        if should_flush and i < len(sentences) - 1:
            chunk_end = sentences[i].end
            chunks.append(doc[current_start:chunk_end])
            current_start = sentences[i + 1].start
            current_pieces = []
            current_word_count = 0

    # Final chunk
    if current_start < len(doc):
        chunks.append(doc[current_start:])

    return chunks if chunks else [doc[:]]





# ---------------------------------------------------------------------------
# Vocabulary extraction (shared between full-doc and per-chunk passes)
# ---------------------------------------------------------------------------
async def _extract_vocabulary(doc, db: PyMongoDatabase) -> list[dict]:
    """
    Extracts enriched vocabulary entries for every content word in the doc.

    Bugs fixed vs previous version:
    - Case mismatch: kaikki stores nouns capitalized ("Freund"), spaCy lemmatizes
      to lowercase ("freund"). Fixed via case-insensitive MongoDB collation query —
      no more guessing capitalization variants manually.
    - spaCy POS errors: when spaCy wrongly tags a noun as VERB, its lemmatizer
      produces a wrong lemma. Fixed by falling back to capitalized lookup when
      the primary lookup fails and the found entry's POS contradicts spaCy's tag.
    - POS disambiguation: when a lemma has multiple dictionary entries (same word,
      different POS), prefer the one matching spaCy's detected POS.
    """

    SPACY_TO_KAIKKI_POS: dict[str, list[str]] = {
        "NOUN": ["noun"],
        "VERB": ["verb"],
        "ADJ": ["adj"],
        "ADV": ["adv"],
        "ADP": ["prep", "postp"],  # mit, durch, für, nach
        "CONJ": ["conj"],  # und, oder
        "SCONJ": ["conj"],  # während, nachdem, obwohl (spaCy splits subordinating)
        "DET": ["det", "article"],  # die, ein, jede
        "PRON": ["pron"],  # seine, welche, er, sie
        "PROPN": ["name", "proper noun"],
        "NUM": ["num"],  # drei, hundert
        "PART": ["particle"],  # nicht, ja, doch, auch, zu (infinitive marker)
        "INTJ": ["intj"],  # ach, oh, nein
        "X": [],  # foreign/unknown — skip, no reliable kaikki match
    }


    # lemma → { spacy_pos, surfaces }
    # spaCy lowercases lemmas — kaikki stores nouns capitalized.
    # We store the spaCy lemma as-is and rely on the collation query
    # to match case-insensitively, then normalise after.
    lemma_info: dict[str, dict] = {}
    for token in doc:
        if not token.is_alpha:
            continue
        lemma = token.lemma_.lower()
        if lemma not in lemma_info:
            lemma_info[lemma] = {"pos": token.pos_, "surfaces": set()}
        lemma_info[lemma]["surfaces"].add(token.text)

    if not lemma_info:
        return []

    # Case-insensitive query via German collation (strength 2).
    # This matches "freund" → "Freund", "mutter" → "Mutter" etc.
    # without us having to guess or produce capitalized variants manually.
    word_to_entries: dict[str, list[dict]] = {}
    async for entry in db.dictionary.find(
        {"word": {"$in": list(lemma_info.keys())}},
        {"word": 1, "pos": 1, "gender": 1, "definitions": 1, "plurals": 1},
        collation={"locale": "de", "strength": 2},
    ):
        # Key by lowercase so our lemma_info keys always match
        word_to_entries.setdefault(entry["word"].lower(), []).append(entry)

    def pick_entry(entries: list[dict], spacy_pos: str) -> dict:
        """Prefer the entry whose POS matches spaCy's tag."""
        preferred = SPACY_TO_KAIKKI_POS.get(spacy_pos, [])
        for entry in entries:
            if entry.get("pos") in preferred:
                return entry
        return entries[0]

    vocabulary: list[dict] = []
    for lemma, info in lemma_info.items():
        entries = word_to_entries.get(lemma)

        # ── Fallback for spaCy POS errors ───────────────────────────────────
        # Common case: spaCy tags a German noun as VERB (e.g. "Hunger"),
        # producing a wrong verb lemma. If we found entries but none match
        # the expected POS, check whether the best entry is actually a noun —
        # if so, trust the dictionary over spaCy's tag.
        if entries:
            best = pick_entry(entries, info["pos"])
            actual_pos = best.get("pos", "")

            spacy_expected = SPACY_TO_KAIKKI_POS.get(info["pos"], [])
            pos_mismatch = actual_pos not in spacy_expected

            if pos_mismatch:
                # Trust the dictionary's POS — spaCy was likely wrong
                info = {**info, "pos": next(
                    (k for k, v in SPACY_TO_KAIKKI_POS.items() if actual_pos in v),
                    info["pos"]
                )}
        else:
            # No entries found at all — skip silently
            # (rare word, proper noun, or spaCy produced a completely wrong lemma)
            continue

        best = pick_entry(entries, info["pos"])

        vocabulary.append({
            "lemma": lemma,
            "surfaces": sorted(info["surfaces"]),
            "pos": best.get("pos", "unknown"),
            "gender": best.get("gender"),
            "plurals": best.get("plurals", []),
            "definitions": best.get("definitions", [])[:3],
            "dict_id": str(best["_id"]),
        })

    return vocabulary
# ---------------------------------------------------------------------------
# Background ingestion pipeline
# ---------------------------------------------------------------------------
async def _run_ingestion(story_id: ObjectId, content: str, db: PyMongoDatabase, uploader: str):
    loop = asyncio.get_event_loop()

    try:
        # ONE nlp pass, total
        full_doc = await loop.run_in_executor(None, partial(nlp, content))

        chunk_spans = _split_into_chunks(full_doc)

        chunk_docs: list[dict] = []
        for idx, span in enumerate(chunk_spans):
            chunk_vocab = await _extract_vocabulary(span, db)  # Span is iterable like Doc
            chunk_word_count = sum(1 for t in span if t.is_alpha)
            chunk_text = span.text_with_ws.strip()  # or span.text

            chunk_docs.append({
                "story_id": story_id,
                "uploader": uploader,
                "chunk_index": idx,
                "content": chunk_text,
                "vocabulary": chunk_vocab,
                "word_count": chunk_word_count,
            })

        if chunk_docs:
            await db.story_chunks.insert_many(chunk_docs)

        # Reuse full_doc — no second pass needed
        difficulty_label, difficulty_score = _compute_difficulty(full_doc)
        total_word_count = sum(1 for t in full_doc if t.is_alpha)
        unique_lemmas = {
            t.lemma_.lower() for t in full_doc
            if t.is_alpha and not t.is_stop
        }

        await db.stories.update_one(
            {"_id": story_id},
            {"$set": {
                "status": "ready",
                "difficulty_label": difficulty_label,
                "difficulty_score": difficulty_score,
                "word_count": total_word_count,
                "unique_word_count": len(unique_lemmas),
                "chunk_count": len(chunk_docs),
            }},
        )

        redis = await get_redis()
        await redis.delete(f"stories:list:{uploader}")

    except Exception as exc:
        await db.stories.update_one(
            {"_id": story_id},
            {"$set": {"status": "failed", "error": str(exc)}},
        )
        raise

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("/upload", status_code=202)
async def upload_story(
        background_tasks: BackgroundTasks,
        title: str = Form(...),
        content: Optional[str] = Form(None),
        file: Optional[UploadFile] = File(None),
        source_url: Optional[str] = Form(None),
        tags: list[str] = Form([]),
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Accepts a story upload immediately (HTTP 202) and processes it in the background.

    Accepts either:
      - `content`: raw German text pasted directly
      - `file`: a UTF-8 .txt file upload

    Returns a story_id the client can use to poll /stories/{id}/status.
    """
    # --- Resolve text source ---
    if file:
        raw_bytes = await file.read()
        try:
            final_content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file must be UTF-8 encoded plain text.",
            )
    elif content:
        final_content = content
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either a 'content' field or upload a file.",
        )

    final_content = final_content.strip()
    if not final_content:
        raise HTTPException(status_code=400, detail="Story content cannot be empty.")
    if len(final_content) > 200_000:
        raise HTTPException(
            status_code=400,
            detail="Story too long. Maximum is 200,000 characters (~30,000 words).",
        )

    # --- Insert stub document immediately ---
    result = await db.stories.insert_one({
        "uploader": current_user,
        "title": title.strip(),
        "source_url": source_url,
        "tags": tags,
        "status": "processing",
        "is_public": False,
        "created_at": datetime.now(timezone.utc),
        # Filled in by the background task:
        "difficulty_label": None,
        "difficulty_score": None,
        "word_count": None,
        "unique_word_count": None,
        "chunk_count": None,
        "error": None,
    })

    # We do NOT store `content` on the story document.
    # Content lives exclusively in story_chunks, keeping the parent lean.
    background_tasks.add_task(
        _run_ingestion,
        result.inserted_id,
        final_content,
        db,
        current_user,
    )

    return {
        "story_id": str(result.inserted_id),
        "status": "processing",
        "message": "Story received and queued for processing.",
    }


@router.get("/{story_id}/status")
async def get_story_status(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Lightweight polling endpoint. The frontend calls this after upload
    until status becomes 'ready' or 'failed'.

    Returns only the fields needed to update the UI — never the full document.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    story = await db.stories.find_one(
        {"_id": oid, "uploader": current_user},
        {"status": 1, "difficulty_label": 1, "chunk_count": 1, "error": 1},
    )
    if not story:
        raise HTTPException(status_code=404, detail="Story not found.")

    return {
        "story_id": story_id,
        "status": story["status"],
        "difficulty_label": story.get("difficulty_label"),
        "chunk_count": story.get("chunk_count"),
        "error": story.get("error"),
    }


@router.get("/get-user-stories", response_model=list[StorySummary])
async def list_stories(
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Returns a summary list of all stories accessible to the current user:
    their own uploads + any public stories from other users.

    Stories still processing or failed are excluded — they have no useful
    metadata to display yet.

    Cached in Redis for 1 hour, invalidated on new upload completion.
    """
    redis = await get_redis()
    cache_key = f"stories:list:{current_user}"

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    # Project away chunk content — we never want that in a list view
    cursor = db.stories.find(
        {
            "status": "ready",  # only show finished stories
            "$or": [{"uploader": current_user}, {"is_public": True}],
        },
        {"difficulty_label": 1, "difficulty_score": 1, "word_count": 1,
         "unique_word_count": 1, "chunk_count": 1, "title": 1,
         "created_at": 1, "is_public": 1, "tags": 1},
    ).sort("created_at", -1)

    stories = []
    async for s in cursor:
        stories.append({
            "id": str(s["_id"]),
            "title": s.get("title", "Untitled"),
            "difficulty_label": s.get("difficulty_label", "Unknown"),
            "difficulty_score": s.get("difficulty_score", 0.0),
            "word_count": s.get("word_count", 0),
            "unique_word_count": s.get("unique_word_count", 0),
            "chunk_count": s.get("chunk_count", 0),
            "created_at": s["created_at"].isoformat(),
            "is_public": s.get("is_public", False),
            "tags": s.get("tags", []),
        })

    await redis.set(cache_key, json.dumps(stories), ex=3600)
    return stories


@router.get("/story/{story_id}", response_model=StoryDetail)
async def get_story(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Returns the full story: metadata + all chunks in order.
    This is the endpoint called when the user opens a story to read it.

    Chunks are sorted by chunk_index so the frontend can render them
    sequentially without needing to sort client-side.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    story = await db.stories.find_one({
        "_id": oid,
        "status": "ready",
        "$or": [{"uploader": current_user}, {"is_public": True}],
    })
    if not story:
        # Distinguish "not found" from "still processing" with a helpful message
        stub = await db.stories.find_one({"_id": oid}, {"status": 1})
        if stub and stub.get("status") == "processing":
            raise HTTPException(status_code=409, detail="Story is still processing.")
        if stub and stub.get("status") == "failed":
            raise HTTPException(
                status_code=422,
                detail=f"Story processing failed: {stub.get('error', 'unknown error')}",
            )
        raise HTTPException(status_code=404, detail="Story not found.")

    # Fetch all chunks for this story, ordered by position
    chunks_cursor = db.story_chunks.find(
        {"story_id": oid},
        {"story_id": 0, "uploader": 0},  # drop internal fields before sending
    ).sort("chunk_index", 1)

    chunks = []
    async for chunk in chunks_cursor:
        chunks.append({
            "chunk_index": chunk["chunk_index"],
            "content": chunk["content"],
            "vocabulary": chunk.get("vocabulary", []),
            "word_count": chunk.get("word_count", 0),
        })

    return {
        "id": str(story["_id"]),
        "title": story.get("title", "Untitled"),
        "difficulty_label": story.get("difficulty_label", "Unknown"),
        "difficulty_score": story.get("difficulty_score", 0.0),
        "word_count": story.get("word_count", 0),
        "unique_word_count": story.get("unique_word_count", 0),
        "chunk_count": story.get("chunk_count", 0),
        "source_url": story.get("source_url"),
        "tags": story.get("tags", []),
        "is_public": story.get("is_public", False),
        "created_at": story["created_at"].isoformat(),
        "chunks": chunks,
    }


@router.get("/{story_id}/chunks/{chunk_index}")
async def get_story_chunk(
        story_id: str,
        chunk_index: int,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    ##################

    ##############

    # Verify the user has access to the parent story first
    story = await db.stories.find_one(
        {
            "_id": oid,
            "status": "ready",
            "$or": [{"uploader": current_user}, {"is_public": True}],
        },
        {"chunk_count": 1},
    )
    if not story:
        raise HTTPException(status_code=404, detail="Story not found.")

    if chunk_index < 0 or chunk_index >= story["chunk_count"]:
        raise HTTPException(
            status_code=404,
            detail=f"Chunk {chunk_index} out of range (0–{story['chunk_count'] - 1}).",
        )

    chunk = await db.story_chunks.find_one(
        {"story_id": oid, "chunk_index": chunk_index},
        {"story_id": 0, "uploader": 0},
    )
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found.")

    print(chunk.get("vocabulary", []))
    return {
        "chunk_index": chunk["chunk_index"],
        "content": chunk["content"],
        "vocabulary": chunk.get("vocabulary", []),
        "word_count": chunk.get("word_count", 0),
    }

@router.delete("/{story_id}", status_code=204)
async def delete_story(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Deletes a story and all its associated chunks.
    Only the uploader can delete their own story.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    result = await db.stories.delete_one({"_id": oid, "uploader": current_user})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Story not found or not yours to delete.")

    await db.story_chunks.delete_many({"story_id": oid})

    redis = await get_redis()
    await redis.delete(f"stories:list:{current_user}")

@router.patch("/{story_id}")
async def update_story(
        story_id: str,
        is_public: Optional[bool] = None,
        tags: Optional[list[str]] = None,
        title: Optional[str] = None,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Partial update for user-editable fields: title, tags, and public visibility.
    Content and vocabulary are immutable after ingestion — re-upload to change them.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    updates: dict = {}
    if is_public is not None:
        updates["is_public"] = is_public
    if tags is not None:
        updates["tags"] = tags
    if title is not None:
        updates["title"] = title.strip()

    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided.")

    result = await db.stories.update_one(
        {"_id": oid, "uploader": current_user},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Story not found or not yours to edit.")

    redis = await get_redis()
    await redis.delete(f"stories:list:{current_user}")

    return {"story_id": story_id, "updated": updates}
