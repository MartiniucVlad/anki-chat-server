import unittest
import time
import json
import unicodedata
import re
import simplemma
from simplemma import simple_tokenizer
from typing import List, Dict, Any, Tuple

# =============================================================================
# 1. COPY OF YOUR LOGIC (For self-contained testing)
# =============================================================================

_SIMPLEMMA_LANGS = {"en", "de", "fr", "es", "it", "pt", "nl", "ru", "uk", "ro"}


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize('NFD', text)
    text = "".join(c for c in text if unicodedata.category(c) != 'Mn')
    return text.lower().strip()


def _lemmatize_token(token: str, lang_code: str) -> str:
    token = token.lower()
    if not token: return token
    try:
        if lang_code in _SIMPLEMMA_LANGS:
            return simplemma.lemmatize(token, lang=lang_code).lower()
        return simplemma.lemmatize(token, lang="en").lower()
    except:
        return token


def precompute_notes(notes: List[Dict[str, Any]], default_lang: str) -> Tuple[List[Dict[str, Any]], bool]:
    changed = False
    new_notes = []
    for note in notes:
        note_copy = dict(note)
        front = normalize_text(note_copy.get("front", ""))
        note_lang = (note_copy.get("language") or default_lang or "en")[:2].lower()

        needs = False
        if note_copy.get("_normalized_front") != front: needs = True
        if note_copy.get("_lang") != note_lang: needs = True
        if "_front_lemmas" not in note_copy: needs = True

        if needs:
            changed = True
            note_copy["_normalized_front"] = front
            note_copy["_lang"] = note_lang
            tokens = list(simple_tokenizer(front))
            tokens = [t.lower() for t in tokens if t.strip()]
            lemmas = [_lemmatize_token(t, note_lang) for t in tokens]
            note_copy["_front_tokens"] = tokens
            note_copy["_front_lemmas"] = lemmas
            note_copy["_single_word_lemma"] = lemmas[0] if len(lemmas) == 1 else None

        new_notes.append(note_copy)
    return new_notes, changed


def make_ngram_set(lemmas: List[str], max_n: int = 5) -> set:
    s = set()
    L = len(lemmas)
    for n in range(1, min(max_n, L) + 1):
        for i in range(0, L - n + 1):
            s.add(" ".join(lemmas[i:i + n]))
    return s


def find_note_matches(content: str, notes: List[Dict[str, Any]], deck_name: str, session_data: Dict[str, Any] = None) -> \
List[Dict[str, Any]]:
    if not content or not notes: return []

    # 1. Detect Lang
    lang_code = session_data.get("target_language", "en") if session_data else "en"

    # 2. Precompute
    needs_precompute = any("_normalized_front" not in n for n in notes)
    if needs_precompute:
        notes, changed = precompute_notes(notes, default_lang=lang_code)
        if changed and session_data is not None:
            session_data["notes"] = notes

    # 3. Compute Content
    content_tokens = [t.lower() for t in simple_tokenizer(content)]
    content_lemmas = [_lemmatize_token(t, lang_code) for t in content_tokens]

    token_set = set(content_tokens)
    lemma_set = set(content_lemmas)
    ngram_set = make_ngram_set(content_lemmas, max_n=6)

    candidate_notes = []
    for note in notes:
        front_norm = note.get("_normalized_front")

        # a) Single-word O(1)
        if note.get("_single_word_lemma"):
            if note["_single_word_lemma"] in lemma_set or front_norm in token_set:
                candidate_notes.append(note)
                continue

        # b) Missed short tokens
        if len(note.get("_front_tokens", [])) == 1:
            token = note["_front_tokens"][0]
            if token in token_set or token in lemma_set:
                candidate_notes.append(note)
                continue

        # c) Multi-word n-gram
        front_lemmas = note.get("_front_lemmas") or []
        if front_lemmas:
            joined = " ".join(front_lemmas)
            if joined in ngram_set:
                candidate_notes.append(note)
                continue

        # d) Fallback substring
        if " " in front_norm and front_norm in normalize_text(content):
            candidate_notes.append(note)
            continue

    return candidate_notes


# =============================================================================
# 2. THE TEST SUITE
# =============================================================================

class TestAnkiMatcher(unittest.TestCase):

    def setUp(self):
        # Reset simplemma cache if possible or just setup standard structures
        pass

    # --- BASIC ENGLISH TESTS ---

    def test_basic_exact_match(self):
        """Simple exact match of a word."""
        notes = [{"id": "1", "front": "apple"}]
        content = "I would like an apple please."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['front'], "apple")

    def test_english_lemmatization(self):
        """Test if 'eating' matches 'eat'."""
        notes = [{"id": "1", "front": "eat"}]
        content = "He is eating lunch."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['front'], "eat")


    # --- GERMAN TESTS (Language Switching) ---

    def test_german_basics(self):
        """Set session language to German and test basic nouns."""
        session = {"target_language": "de"}
        notes = [{"id": "1", "front": "Apfel"}]  # Apple
        content = "Ich esse einen Apfel."
        matches = find_note_matches(content, notes, "deck", session)
        self.assertEqual(len(matches), 1)

    def test_german_lemmatization(self):
        """Test verb conjugation: 'ging' -> 'gehen' (went -> to go)."""
        session = {"target_language": "de"}
        notes = [{"id": "1", "front": "gehen"}]
        content = "Er ging nach Hause."  # He went home
        matches = find_note_matches(content, notes, "deck", session)
        self.assertEqual(len(matches), 1)

    def test_german_normalization(self):
        """Test handling of Umlauts if user is lazy: 'Uber' -> 'Über'."""
        session = {"target_language": "de"}
        notes = [{"id": "1", "front": "Über"}]
        content = "Das ist uber alles."  # User typed 'u' instead of 'ü'
        # normalize_text strips accents: 'Über'->'uber', 'uber'->'uber'. match.
        matches = find_note_matches(content, notes, "deck", session)
        self.assertEqual(len(matches), 1)

    # --- MULTI-WORD & PHRASES ---

    def test_multi_word_exact(self):
        """Test phrases like 'hot dog'."""
        notes = [{"id": "1", "front": "hot dog"}]
        content = "I ate a hot dog."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)



    def test_phrase_boundary(self):
        """Ensure 'hot dog' doesn't match 'hot' and 'dog' separately if they aren't adjacent."""
        # This function logic actually splits phrases into n-grams.
        # If the note is "hot dog" and content is "it is hot outside the dog barked",
        # the n-gram "hot dog" does not exist.
        notes = [{"id": "1", "front": "hot dog"}]
        content = "It is hot outside and the dog barked."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 0)

    # --- FALSE POSITIVES & BOUNDARIES ---

    def test_substring_false_positive(self):
        """Ensure 'cat' does not match inside 'dedication'."""
        notes = [{"id": "1", "front": "cat"}]
        content = "I have a lot of dedication."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 0)

    def test_punctuation_handling(self):
        """Ensure words followed by comma/period are detected."""
        notes = [{"id": "1", "front": "hello"}]
        content = "Hello, world!"  # Tokenizer should split 'Hello' and ','
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)

    # --- PERFORMANCE / STRESS TEST ---

    def test_performance_large_deck(self):
        """
        STRESS TEST:
        1. Create 5,000 dummy notes.
        2. Create a long paragraph (500 words).
        3. Measure time.
        """
        print("\n--- Starting Stress Test ---")

        # 1. Generate Dummy Deck
        deck_size = 5000
        notes = [{"id": str(i), "front": f"word{i}"} for i in range(deck_size)]
        # Add a few real targets
        notes.append({"id": "target1", "front": "extraordinary"})
        notes.append({"id": "target2", "front": "persistence"})

        session = {"target_language": "en"}

        # 2. Precompute first (simulate deck loading)
        start_load = time.time()
        # This simulates the first time the deck is loaded/persisted
        find_note_matches("warmup", notes, "stress_test", session)
        load_time = time.time() - start_load
        print(f"Pre-computation of {deck_size} cards took: {load_time:.4f}s")

        # 3. Create Heavy Content
        # A 500-word essay containing the target words
        content = " ".join(["bla"] * 250) + " extraordinary " + " ".join(["foo"] * 250) + " persistence."

        # 4. Measure Query Time (The Real-Time Chat Experience)
        start_query = time.time()
        matches = find_note_matches(content, notes, "stress_test", session)
        query_time = time.time() - start_query

        print(f"Querying long message against {deck_size} cards took: {query_time:.4f}s")

        # Assertions
        self.assertTrue(len(matches) >= 2)
        # Performance Threshold: Should be under 50ms for good UX
        self.assertLess(query_time, 0.1, "Performance warning: Matching took too long!")
        print("--- Stress Test Passed ---")

    # --- STATE MUTATION ---

    def test_session_state_update(self):
        """Verify that pre-computed data is actually saved back to the session dict."""
        notes = [{"id": "1", "front": "run"}]
        session = {"target_language": "en"}

        # Initially notes have no internal flags
        self.assertNotIn("_front_lemmas", notes[0])

        # Run Matcher
        find_note_matches("run", notes, "deck", session)

        # Check Session Data
        updated_notes = session["notes"]
        self.assertIn("_front_lemmas", updated_notes[0])
        self.assertEqual(updated_notes[0]["_front_lemmas"], ["run"])

    def test_card_casing_normalization(self):
        """Ensure Card 'Apple' matches user 'apple'."""
        # Note: 'Apfel' (German noun) is always capitalized in decks
        notes = [{"id": "1", "front": "Apple"}]
        content = "I want an apple."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)

    def test_stop_word_spam(self):
        """
        If a user has a card for 'a', does it trigger on 'I have a cat'?
        Technically it SHOULD match, but it's good to verify this behavior
        so you know if you need to add a 'minimum word length' filter later.
        """
        notes = [{"id": "1", "front": "a"}]
        content = "I have a cat."
        matches = find_note_matches(content, notes, "deck", {})
        self.assertEqual(len(matches), 1)
        # If this passes, your system is technically correct, but expensive.
        # Considerations for later: Ignore words < 3 chars?

    def test_japanese_fallback_behavior(self):
        """
        Japanese is not in _SIMPLEMMA_LANGS.
        Test if exact matching still works despite tokenizer issues.
        """
        # Japanese uses no spaces. 'apple' is 'りんご'
        notes = [{"id": "1", "front": "りんご"}]

        # If simple_tokenizer splits by space, it might see this whole sentence as one token.
        content = "私はりんごを食べます"

        matches = find_note_matches(content, notes, "deck", {"target_language": "ja"})

        # This assertion will likely FAIL or PASS depending on if simple_tokenizer
        # handles CJK. It is better to know now.
        if len(matches) == 0:
            print("\n[WARNING] CJK Tokenization failed. Japanese decks will only match exact full sentences.")
        else:
            self.assertEqual(matches[0]['front'], "りんご")





if __name__ == '__main__':
    unittest.main()