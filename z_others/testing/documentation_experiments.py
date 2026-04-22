import unittest
import time
import json
import unicodedata
import re
import simplemma
from simplemma import simple_tokenizer
from typing import List, Dict, Any, Tuple


# =============================================================================
# 1. CORE LOGIC (LinguistChat NLP Module)
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
        # We enforce greedy=True based on previous optimization for irregular verbs
        if lang_code in _SIMPLEMMA_LANGS:
            return simplemma.lemmatize(token, lang=lang_code, greedy=True).lower()
        return simplemma.lemmatize(token, lang="en", greedy=True).lower()
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
            # Optimization for O(1) lookup
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

    lang_code = session_data.get("target_language", "en") if session_data else "en"

    # Precompute step
    needs_precompute = any("_normalized_front" not in n for n in notes)
    if needs_precompute:
        notes, changed = precompute_notes(notes, default_lang=lang_code)
        if changed and session_data is not None:
            session_data["notes"] = notes

    # Content processing
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
# 2. EXPERIMENT SUITE (Data Generation)
# =============================================================================

class ExperimentSuite(unittest.TestCase):

    def log_result(self, test_name, input_val, target_val, result, extra=""):
        """Helper to print formatted rows for the report"""
        status = "MATCH" if result else "NO MATCH"
        print(f"| {test_name:<15} | {input_val:<25} | {target_val:<15} | {status:<10} | {extra}")

    def print_header(self, title):
        print(f"\n\n=== {title} ===")
        print(f"| {'Category':<15} | {'User Input':<25} | {'Card Target':<15} | {'Result':<10} | {'Notes'}")
        print("-" * 90)

    # ------------------------------------------------------------------------
    # EXPERIMENT 1: Multilingual Accuracy & Lemmatization
    # ------------------------------------------------------------------------
    def test_exp_1_multilingual(self):
        self.print_header("EXPERIMENT 1: Multilingual Morphology")

        scenarios = [
            # ENGLISH
            ("en", "Continuous", "I am eating", "eat", True),
            ("en", "Regular Plural", "Look at the cars", "car", True),
            # GERMAN
            ("de", "Noun Case", "Ich sehe den Zug", "Zug", True),  # Accusative
            ("de", "Verb Conj.", "Wir gingen nach Hause", "gehen", True),  # Präteritum
            ("de", "Umlaut Norm.", "Er ist funf Jahre", "fünf", True),  # User missed umlaut
            # FRENCH
            ("fr", "Past Participle", "J'ai mangé la pomme", "manger", True),
            ("fr", "Plural Adj", "Ils sont grands", "grand", True),
            # SPANISH
            ("es", "Irregular Verb", "Yo tengo un gato", "tener", True),
            ("es", "Gender/Num", "Las casas rojas", "casa", True),
        ]

        for lang, category, content, card_front, should_match in scenarios:
            session = {"target_language": lang}
            notes = [{"id": "1", "front": card_front}]

            matches = find_note_matches(content, notes, "exp1", session)
            match_found = len(matches) > 0

            self.log_result(f"{lang.upper()}-{category}", content, card_front, match_found)

            if should_match:
                self.assertTrue(match_found, f"Failed {lang} test: {content} -> {card_front}")
            else:
                self.assertFalse(match_found)

    # ------------------------------------------------------------------------
    # EXPERIMENT 2: False Positives & Boundaries
    # ------------------------------------------------------------------------
    def test_exp_2_false_positives(self):
        self.print_header("EXPERIMENT 2: False Positive Analysis")

        scenarios = [
            # Type, Input, Card, Should Match?
            ("Substring", "I have dedication", "cat", False),  # cat inside dedication
            ("Compound", "Hotdog stand", "dog", False),  # dog inside Hotdog (depends on tokenizer)
            ("Punctuation", "Hello, world!", "hello", True),
            ("Stop Word", "This is a test", "a", True),  # Technically a match, but might be spammy
        ]

        for cat, content, card_front, expected in scenarios:
            notes = [{"id": "1", "front": card_front}]
            matches = find_note_matches(content, notes, "exp2", {})
            match_found = len(matches) > 0

            self.log_result(cat, content, card_front, match_found, f"Expected: {expected}")
            self.assertEqual(match_found, expected)

    # ------------------------------------------------------------------------
    # EXPERIMENT 3: Performance Scaling (Stress Test)
    # ------------------------------------------------------------------------
    def test_exp_3_performance(self):
        print(f"\n\n=== EXPERIMENT 3: Performance Scaling ===")
        print(f"| {'Deck Size':<10} | {'Precompute Time (s)':<20} | {'Query Time (s)':<15} | {'Notes'}")
        print("-" * 70)

        sizes = [10, 100, 1000, 5000, 10000]

        for size in sizes:
            # 1. Generate Deck
            notes = [{"id": str(i), "front": f"word{i}"} for i in range(size)]
            # Add a target at the very end to ensure full traversal
            target_word = "persistence"
            notes.append({"id": "target", "front": target_word})

            # 2. Measure Precomputation (Simulates first load)
            session = {"target_language": "en"}
            t0 = time.perf_counter()
            find_note_matches("warmup", notes, "stress", session)
            t1 = time.perf_counter()
            precompute_time = t1 - t0

            # 3. Measure Query (Simulates chat message)
            # Create a heavy message: 200 words of junk + target + 200 words junk
            content = " ".join(["junk"] * 200) + f" {target_word} " + " ".join(["trash"] * 200)

            t2 = time.perf_counter()
            matches = find_note_matches(content, notes, "stress", session)
            t3 = time.perf_counter()
            query_time = t3 - t2

            print(
                f"| {size:<10} | {precompute_time:.5f}               | {query_time:.5f}          | Found: {len(matches)}")

            # Assertions for documentation validity
            self.assertLess(query_time, 1, f"Query too slow for {size} cards")

    # ------------------------------------------------------------------------
    # EXPERIMENT 4: CJK (Chinese/Japanese) Limitations
    # ------------------------------------------------------------------------
    def test_exp_4_cjk_limitations(self):
        self.print_header("EXPERIMENT 4: CJK Limitation Test")

        # Scenario: Japanese "Apple" (Ringo)
        # Note: simple_tokenizer splits by space. Japanese has no spaces.
        # This test documents EXACTLY how the system behaves (fail or pass).

        content = "私はりんごを食べます"  # "I eat apple"
        card_front = "りんご"  # "apple"

        notes = [{"id": "1", "front": card_front}]
        session = {"target_language": "ja"}

        matches = find_note_matches(content, notes, "exp4", session)
        match_found = len(matches) > 0

        self.log_result("Japanese", content, card_front, match_found, "Tokenizer Limitation Check")

        if not match_found:
            print("\n   -> [INFO] System correctly failed to tokenize Japanese without spaces.")
            print("   -> Recommendation: Add MeCab or specialized tokenizer for 'ja' in future.")
        else:
            print("\n   -> [INFO] Surprise match! Simplemma might have updated its CJK handling.")


if __name__ == '__main__':
    unittest.main()