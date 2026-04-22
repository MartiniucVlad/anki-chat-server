import json
import asyncio
import time
import os

from database_clients.database_mongo import connect_to_mongo, close_mongo_connection, get_db
from config import settings

FILE_PATH = "kaikki.org-dictionary-German.jsonl"
BATCH_SIZE = 5000

# Expanded POS list — covers all word classes useful for a language learner.
# Excluded: "character", "symbol", "punct", "affix", "combining-form"
# (these are not useful for reading comprehension)
ALLOWED_POS = {
    "noun", "verb", "adj", "adv",
    "prep",       # prepositions: mit, durch, für
    "conj",       # conjunctions: und, oder, während, nachdem
    "pron",       # pronouns: seine, welche, er, sie
    "det",        # determiners: die, ein, jede
    "particle",   # particles: nicht, ja, doch, auch
    "intj",       # interjections: ach, oh, nein
    "num",        # numerals: drei, hundert
    "article",    # articles if kaikki separates them
}


async def setup_indexes(db):
    print("Setting up database indexes...")
    collection = db["dictionary"]

    # Drop and recreate for a clean run
    await collection.drop_indexes()

    # Standard exact-match index
    await collection.create_index("word")
    await collection.create_index("pos")

    # Case-insensitive German collation index —
    # this is what makes the vocabulary extraction query work correctly,
    # matching spaCy's lowercase lemmas to kaikki's capitalized nouns
    await collection.create_index(
        [("word", 1)],
        name="word_ci",
        collation={"locale": "de", "strength": 2},
    )

    print("Indexes created.")


def extract_glosses(raw_entry: dict) -> list[str]:
    """
    Pulls all usable gloss strings from a kaikki entry.

    kaikki nests definitions under senses → glosses (list of strings).
    Some entries also have a top-level "glosses" key — we check both.
    We skip glosses that are pure meta-text like "used in", "see also" etc.
    """
    glosses: list[str] = []
    seen: set[str] = set()

    SKIP_PREFIXES = (
        "used in", "see ", "abbreviation", "initialism",
        "alternative", "obsolete", "misspelling",
    )

    def add(g: str):
        g = g.strip()
        if not g:
            return
        if any(g.lower().startswith(p) for p in SKIP_PREFIXES):
            return
        if g not in seen:
            glosses.append(g)
            seen.add(g)

    for sense in raw_entry.get("senses", []):
        for g in sense.get("glosses", []):
            add(g)
        # Some kaikki entries put a single gloss at sense level
        if "gloss" in sense:
            add(sense["gloss"])

    # Fallback: top-level glosses key (rare but exists)
    for g in raw_entry.get("glosses", []):
        add(g)

    return glosses[:5]  # cap at 5 — more than 3 useful for function words


def extract_gender(raw_entry: dict) -> str | None:
    """
    Gender can appear in sense tags OR in the head_templates data.
    Check both so we don't miss nouns where it's stored differently.
    """
    # Method 1: sense tags (most common)
    first_tags = raw_entry.get("senses", [{}])[0].get("tags", [])
    if "masculine" in first_tags:   return "der"
    if "feminine" in first_tags:    return "die"
    if "neuter" in first_tags:      return "das"

    # Method 2: head_templates expansion (backup)
    for ht in raw_entry.get("head_templates", []):
        args = ht.get("args", {})
        g = args.get("g") or args.get("1")
        if g == "m":    return "der"
        if g == "f":    return "die"
        if g == "n":    return "das"

    return None


def extract_plurals(raw_entry: dict) -> list[str]:
    plurals: set[str] = set()
    for form in raw_entry.get("forms", []):
        tags = form.get("tags", [])
        form_str = form.get("form", "").strip()
        if "plural" in tags and form_str and form_str != "-":
            plurals.add(form_str)
    return sorted(plurals)


def extract_word_data(raw_entry: dict) -> dict | None:
    word = raw_entry.get("word", "").strip()
    pos  = raw_entry.get("pos", "").strip()

    if not word:
        return None

    # Skip multi-word expressions — they don't match single-token lookups
    if " " in word:
        return None

    # Skip entries with no usable definitions
    glosses = extract_glosses(raw_entry)
    if not glosses:
        return None

    gender  = extract_gender(raw_entry) if pos == "noun" else None
    plurals = extract_plurals(raw_entry) if pos == "noun" else []

    # For function words (prep, conj, pron, det, particle) also extract
    # any usage notes from the raw entry — these are often more useful
    # than glosses for understanding how the word is used
    tags: list[str] = []
    for sense in raw_entry.get("senses", []):
        tags.extend(sense.get("tags", []))
    tags = list(set(tags))  # deduplicate

    return {
        "word":        word,
        "pos":         pos,
        "gender":      gender,
        "plurals":     plurals,
        "definitions": glosses,
        "tags":        tags,    # grammatical tags — useful for function words
    }


async def ingest_data():
    await connect_to_mongo()
    db = get_db()

    if db is None:
        print("Failed to connect to DB.")
        return

    collection = db["dictionary"]

    print("Clearing old dictionary data...")
    await collection.delete_many({})
    await setup_indexes(db)

    if not os.path.exists(FILE_PATH):
        print(f"Error: {FILE_PATH} not found.")
        await close_mongo_connection()
        return

    print(f"Starting ingestion from {FILE_PATH}...")
    start_time = time.time()

    batch: list[dict] = []
    total_inserted = 0
    skipped_pos = 0
    skipped_no_def = 0
    errors = 0

    pos_counts: dict[str, int] = {}

    with open(FILE_PATH, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            try:
                raw_entry = json.loads(line)
                pos = raw_entry.get("pos", "")

                if pos not in ALLOWED_POS:
                    skipped_pos += 1
                    continue

                clean = extract_word_data(raw_entry)
                if not clean:
                    skipped_no_def += 1
                    continue

                pos_counts[pos] = pos_counts.get(pos, 0) + 1
                batch.append(clean)

                if len(batch) >= BATCH_SIZE:
                    await collection.insert_many(batch, ordered=False)
                    total_inserted += len(batch)
                    batch = []
                    print(f"  Inserted {total_inserted:,} words... (line {line_number:,})")

            except json.JSONDecodeError:
                errors += 1
            except Exception as e:
                errors += 1
                if errors < 10:  # don't flood logs
                    print(f"  Error on line {line_number}: {e}")

    if batch:
        await collection.insert_many(batch, ordered=False)
        total_inserted += len(batch)

    elapsed = round(time.time() - start_time, 2)

    print("\n" + "─" * 50)
    print("INGESTION COMPLETE")
    print(f"  Total inserted : {total_inserted:,}")
    print(f"  Skipped (POS)  : {skipped_pos:,}")
    print(f"  Skipped (no def): {skipped_no_def:,}")
    print(f"  Parse errors   : {errors}")
    print(f"  Time           : {elapsed}s")
    print("\nBreakdown by POS:")
    for p, count in sorted(pos_counts.items(), key=lambda x: -x[1]):
        print(f"  {p:<12} {count:,}")
    print("─" * 50)

    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(ingest_data())