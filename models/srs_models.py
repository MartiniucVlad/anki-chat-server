# backend/models/srs_models.py

from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


# ── Card structure ────────────────────────────────────────────────────────────

class CardFront(BaseModel):
    """The prompt side of a flashcard — what the learner sees."""
    word: str
    context_sentence: Optional[str] = None   # sentence with "___" blank
    part_of_speech: Optional[str] = None


class CardBack(BaseModel):
    """The answer side — revealed after the learner responds."""
    definition: str
    gender: Optional[str] = None             # der / die / das (nouns)
    plural: Optional[str] = None
    example_sentence: Optional[str] = None   # full German sentence
    synonyms: list[str] = Field(default_factory=list)
    form_info: Optional[str] = None          # e.g. "past participle of gehen"


# ── Request models ────────────────────────────────────────────────────────────

class CreateCardRequest(BaseModel):
    front: CardFront
    back: CardBack
    source_type: Optional[str] = None        # "chat" | "story" | "manual"
    source_id: Optional[str] = None
    lemma: Optional[str] = None              # normalised lower-case; derived if missing


class BulkCreateCardsRequest(BaseModel):
    cards: list[CreateCardRequest]
    skip_duplicates: bool = True


class StoryVocabCardRequest(BaseModel):
    """Create a card directly from a story chunk's pre-extracted vocabulary."""
    story_id: str
    chunk_index: int
    lemma: str
    source_type: str = "story"


class CreateDeckRequest(BaseModel):
    name: str
    description: Optional[str] = None
    language: str = "de"


class UpdateDeckRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SubmitReviewRequest(BaseModel):
    rating: int = Field(ge=1, le=4, description="1=Again 2=Hard 3=Good 4=Easy")
    review_duration_ms: Optional[int] = None


# ── Response models ───────────────────────────────────────────────────────────

class CardResponse(BaseModel):
    card_id: str
    deck_id: str
    front: CardFront
    back: CardBack
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    lemma: str
    state: int                              # 1=Learning 2=Review 3=Relearning
    state_label: str
    due: datetime
    stability: float
    difficulty: float
    reps: int
    lapses: int
    retrievability: float
    created_at: datetime
    updated_at: datetime


class DeckResponse(BaseModel):
    deck_id: str
    name: str
    description: Optional[str]
    language: str
    card_count: int
    due_count: int
    new_count: int
    created_at: datetime
    updated_at: datetime


class ReviewSessionResponse(BaseModel):
    deck_id: str
    cards: list[CardResponse]
    total_due: int
    returned: int


class ReviewResultResponse(BaseModel):
    card: CardResponse
    scheduling: dict


class DeckStatsResponse(BaseModel):
    deck_id: str
    deck_name: str
    total_cards: int
    due_now: int
    new_cards: int
    learning_cards: int
    review_cards: int
    relearning_cards: int
    average_difficulty: float
    average_stability: float
    total_reviews: int
    mature_cards: int                       # reps >= 3


class GlobalStatsResponse(BaseModel):
    total_decks: int
    total_cards: int
    total_due: int
    total_reviews: int
    cards_added_today: int
    reviews_today: int


class BulkCreateResult(BaseModel):
    created: int
    skipped_duplicates: int
    card_ids: list[str]


class OptimizeResponse(BaseModel):
    status: str
    message: str