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
import re

# ── helpers ──────────────────────────────────────────────────────────────────

_FORM_OF_RE = re.compile(
    r'(?:past participle|present participle|inflection|plural|genitive|'
    r'dative|accusative|nominative|comparative|superlative)\s+of\s+([^\s:,;]+)',
    re.IGNORECASE,
)


def _extract_base_from_form_defs(definitions: list[str]) -> str | None:
    """
    Given definitions like ["past participle of ziehen"] or
    ["inflection of sonderbar:\nnominative masculine singular"],
    return the base lemma string ("ziehen", "sonderbar"), or None.
    """
    for defn in definitions:
        m = _FORM_OF_RE.search(defn)
        if m:
            base = m.group(1).strip(" :,;")
            if base:
                return base
    return None

async def get_synonyms(db, word: str) -> list[str]:
    doc = await db.synonyms.find_one({"words": word})
    if not doc:
        return []
    return [w for w in doc["words"] if w != word]


# ── main function ─────────────────────────────────────────────────────────────

async def extract_vocabulary(doc, db: PyMongoDatabase) -> list[dict]:
    """
    Extracts enriched vocabulary entries for every content word in the doc.

    Bugs fixed vs previous version:
    - Case mismatch: kaikki stores nouns capitalized ("Freund"), spaCy lemmatizes
      to lowercase ("freund"). Fixed via case-insensitive MongoDB collation query.
    - spaCy POS errors: capitalized fallback lookup when primary fails.
    - POS disambiguation: prefer entry whose POS matches spaCy's tag.
    - Form enrichment: inflected surfaces are looked up; their form-of definitions
      are shown before the lemma definitions.
    - Form-of base fallback: when spaCy's lemma lookup finds nothing, parse the
      base word out of the form-of definition string ("inflection of sonderbar")
      and look it up directly in the DB.
    """

    SPACY_TO_KAIKKI_POS: dict[str, list[str]] = {
        "NOUN": ["noun"],
        "VERB": ["verb"],
        "ADJ": ["adj"],
        "ADV": ["adv"],
        "ADP": ["prep", "postp"],
        "CONJ": ["conj"],
        "SCONJ": ["conj"],
        "DET": ["det", "article"],
        "PRON": ["pron"],
        "PROPN": ["name", "proper noun"],
        "NUM": ["num"],
        "PART": ["particle"],
        "INTJ": ["intj"],
        "X": [],
    }

    # lemma → { spacy_pos, surfaces }
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

    # surface_lower → lemma (only surfaces that differ from their lemma)
    surface_to_lemma: dict[str, str] = {}
    for lemma, info in lemma_info.items():
        for surface in info["surfaces"]:
            surface_lower = surface.lower()
            if surface_lower != lemma:
                surface_to_lemma[surface_lower] = lemma

    all_lookup_words = list(set(list(lemma_info.keys()) + list(surface_to_lemma.keys())))

    # ── first DB round-trip: lemmas + surfaces ────────────────────────────────
    word_to_entries: dict[str, list[dict]] = {}
    async for entry in db.dictionary.find(
            {"word": {"$in": all_lookup_words}},
            {"word": 1, "pos": 1, "gender": 1, "definitions": 1, "plurals": 1, "tags": 1},
            collation={"locale": "de", "strength": 2},
    ):
        word_to_entries.setdefault(entry["word"].lower(), []).append(entry)

    # form-of entries keyed by surface_lower
    form_entries: dict[str, list[dict]] = {}
    for surface_lower in surface_to_lemma:
        candidates = word_to_entries.get(surface_lower, [])
        form_of = [e for e in candidates if "form-of" in e.get("tags", [])]
        if form_of:
            form_entries[surface_lower] = form_of

    # ── collect base words that need a second lookup ──────────────────────────
    # When spaCy's lemma won't resolve but the form-of definition names the base
    # (e.g. "inflection of sonderbar"), we need to fetch that base from the DB.
    extra_base_lookups: dict[str, str] = {}  # base_lower → original surface_lower
    for surface_lower, form_list in form_entries.items():
        lemma = surface_to_lemma[surface_lower]
        # Only bother if the lemma itself has no entry in the DB
        if word_to_entries.get(lemma):
            continue
        for fe in form_list:
            base = _extract_base_from_form_defs(fe.get("definitions", []))
            if base:
                extra_base_lookups[base.lower()] = surface_lower
                break  # one base per surface is enough

    # ── second DB round-trip (only when needed) ───────────────────────────────
    if extra_base_lookups:
        async for entry in db.dictionary.find(
                {"word": {"$in": list(extra_base_lookups.keys())}},
                {"word": 1, "pos": 1, "gender": 1, "definitions": 1, "plurals": 1, "tags": 1},
                collation={"locale": "de", "strength": 2},
        ):
            word_to_entries.setdefault(entry["word"].lower(), []).append(entry)

    # ── build vocabulary ──────────────────────────────────────────────────────
    vocabulary: list[dict] = []

    for lemma, info in lemma_info.items():
        spacy_pos = info["pos"]
        surfaces = info["surfaces"]
        preferred_pos_tags = SPACY_TO_KAIKKI_POS.get(spacy_pos, [])

        # --- resolve the best form-of entry for this lemma group ---
        form_entry: dict | None = None
        for surface in surfaces:
            surface_lower = surface.lower()
            if surface_lower == lemma:
                continue
            candidates_form = form_entries.get(surface_lower, [])
            if not candidates_form:
                continue
            fe = next(
                (e for e in candidates_form if e.get("pos") in preferred_pos_tags),
                candidates_form[0],
            )
            form_entry = fe
            break

        # --- resolve the base lemma entry ---
        candidates = word_to_entries.get(lemma, [])
        matched = [e for e in candidates if e.get("pos") in preferred_pos_tags]
        if not matched:
            matched = candidates

        # Capitalised noun fallback
        if not matched:
            candidates_cap = word_to_entries.get(lemma.capitalize().lower(), [])
            matched = (
                    [e for e in candidates_cap if e.get("pos") in preferred_pos_tags]
                    or candidates_cap
            )

        # ── NEW: form-of base fallback ────────────────────────────────────────
        # spaCy gave us a lemma that isn't in the dictionary, but the form-of
        # definition tells us the real base word — use that instead.
        if not matched and form_entry:
            base = _extract_base_from_form_defs(form_entry.get("definitions", []))
            if base:
                base_lower = base.lower()
                candidates_base = word_to_entries.get(base_lower, [])
                matched = (
                        [e for e in candidates_base if e.get("pos") in preferred_pos_tags]
                        or candidates_base
                )
                if matched:
                    # Rewrite lemma to the real base so the UI shows "sonderbar"
                    # instead of spaCy's wrong "sonderbaren"
                    lemma = base_lower
        # ─────────────────────────────────────────────────────────────────────

        if not matched:
            continue

        best = next((e for e in matched if e.get("pos") in preferred_pos_tags), matched[0])

        entry_out: dict = {
            "lemma": lemma,
            "pos": best.get("pos"),
            "gender": best.get("gender"),
            "plurals": best.get("plurals", []),
            "definitions": best.get("definitions", []),
            "surfaces": list(surfaces),
            "synonyms": await get_synonyms(db, lemma),
        }

        if form_entry:
            entry_out["form_word"] = form_entry["word"]
            entry_out["form_definitions"] = form_entry.get("definitions", [])

        vocabulary.append(entry_out)

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
            chunk_vocab = await extract_vocabulary(span, db)  # Span is iterable like Doc
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
