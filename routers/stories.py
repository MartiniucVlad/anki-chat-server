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

from stories.difficulty_grading import _compute_difficulty
from stories.story_processing import _run_ingestion

router = APIRouter(tags=["Stories"], prefix="/stories")


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


async def extract_text_from_file(file: UploadFile) -> str:
    raw_bytes = await file.read()
    filename = (file.filename or '').lower()

    if filename.endswith('.txt'):
        try:
            return raw_bytes.decode('utf-8')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Text file must be UTF-8 encoded.")

    elif filename.endswith('.pdf'):
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw_bytes))
            pages = [page.extract_text() for page in reader.pages if page.extract_text()]
            if not pages:
                raise HTTPException(status_code=400,
                                    detail="Could not extract text from PDF. It may be scanned/image-based.")
            return '\n\n'.join(pages)
        except ImportError:
            raise HTTPException(status_code=500, detail="PDF support not installed. Run: pip install pypdf")

    elif filename.endswith('.epub'):
        try:
            import io
            import ebooklib
            from ebooklib import epub
            from bs4 import BeautifulSoup
            book = epub.read_epub(io.BytesIO(raw_bytes))
            texts = []
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                text = soup.get_text(separator='\n')
                if text.strip():
                    texts.append(text.strip())
            if not texts:
                raise HTTPException(status_code=400, detail="Could not extract text from EPUB.")
            return '\n\n'.join(texts)
        except ImportError:
            raise HTTPException(status_code=500,
                                detail="EPUB support not installed. Run: pip install ebooklib beautifulsoup4")

    else:
        raise HTTPException(status_code=400, detail="Unsupported file type. Use .txt, .pdf, or .epub.")


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
    if file and file.filename:
        final_content = await extract_text_from_file(file)
    elif content:
        final_content = content
    else:
        raise HTTPException(status_code=400, detail="Provide either a 'content' field or upload a file.")

    final_content = final_content.strip()
    if not final_content:
        raise HTTPException(status_code=400, detail="Story content cannot be empty.")
    if len(final_content) > 1_000_000:
        raise HTTPException(status_code=400, detail="Story too long. Maximum is 200,000 characters (~30,000 words).")

    # --- Insert stub document immediately ---
    result = await db.stories.insert_one({
        "subscribers": [current_user],  # creator is the first subscriber
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
        {"_id": oid, "subscribers": current_user},
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
    stories they are subscribed to + any public stories from other users.

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
            "$or": [{"subscribers": current_user}, {"is_public": True}],
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
        "$or": [{"subscribers": current_user}, {"is_public": True}],
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
        {"story_id": 0, "subscribers": 0},  # drop internal fields before sending
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

    # Verify the user has access to the parent story first
    story = await db.stories.find_one(
        {
            "_id": oid,
            "status": "ready",
            "$or": [{"subscribers": current_user}, {"is_public": True}],
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
        {"story_id": 0, "subscribers": 0},
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
    Removes the current user from the story's subscriber list.
    The story and its chunks are only fully deleted when no subscribers remain.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    # Remove current_user from subscribers list (only if they're in it)
    result = await db.stories.update_one(
        {"_id": oid, "subscribers": current_user},
        {"$pull": {"subscribers": current_user}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Story not found or you are not subscribed.")

    # Check if any subscribers remain
    story = await db.stories.find_one({"_id": oid}, {"subscribers": 1})
    if story and len(story.get("subscribers", [])) == 0:
        await db.stories.delete_one({"_id": oid})
        await db.story_chunks.delete_many({"story_id": oid})

    redis = await get_redis()
    await redis.delete(f"stories:list:{current_user}")



@router.post("/{story_id}/subscribe", status_code=200)
async def subscribe_to_story(
        story_id: str,
        db: PyMongoDatabase = Depends(get_db),
        current_user: str = Depends(get_current_user),
):
    """
    Adds the current user to a story's subscriber list.
    Only works for public stories or stories the user already has access to.
    """
    try:
        oid = ObjectId(story_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid story ID format.")

    story = await db.stories.find_one({"_id": oid}, {"_id": 1})
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")

    result = await db.stories.update_one(
        {"_id": oid, "subscribers": {"$ne": current_user}},
        {"$push": {"subscribers": current_user}},
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=409, detail="Already subscribed to this story.")

    redis = await get_redis()
    await redis.delete(f"stories:list:{current_user}")

    return {"story_id": story_id, "message": "Successfully subscribed."}

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
        {"_id": oid, "subscribers": current_user},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Story not found or not yours to edit.")

    redis = await get_redis()
    await redis.delete(f"stories:list:{current_user}")

    return {"story_id": story_id, "updated": updates}


from fastapi.responses import StreamingResponse
from ollama import chat as ollama_chat


class ExplainRequest(BaseModel):
    selected_text: str
    story_id: Optional[str] = None  # for future context injection


@router.post("/explain")
async def explain_selection(
        req: ExplainRequest,
        current_user: str = Depends(get_current_user),
):
    if not req.selected_text.strip():
        raise HTTPException(status_code=400, detail="No text provided.")
    if len(req.selected_text) > 2000:
        raise HTTPException(status_code=400, detail="Selection too long.")

    system_prompt = """You are a German language tutor embedded in a reading app.
The user has selected a passage from a German text they are reading.
Explain it clearly and helpfully. Cover:
- What the passage means in natural English
- Any interesting grammar structures (cases, verb forms, word order)
- Any vocabulary worth highlighting (idioms, compound words, tricky words)
Keep it concise — 3 to 6 sentences. Use simple English. Do not repeat the German text back in full."""

    user_message = f'The user selected this German text:\n\n"{req.selected_text}"\n\nPlease explain it.'

    def generate():
        stream = ollama_chat(
            model="kimi-k2.5:cloud",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            stream=True,
        )
        for chunk in stream:
            content = chunk.message.content
            if content:
                yield content

    return StreamingResponse(generate(), media_type="text/plain")