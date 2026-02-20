import json
import asyncio
import time
import os

# --- IMPORTS FROM YOUR PROJECT ---
# Assuming your database file is named 'database.py' and config is 'config.py'
from database_clients.database_mongo import connect_to_mongo, close_mongo_connection, get_db
from config import settings

# FILE CONSTANTS
FILE_PATH = "kaikki.org-dictionary-German.jsonl"  # Make sure this file exists
BATCH_SIZE = 5000


async def setup_indexes(db):
    """Creates an index on the 'word' field so lookups take 0.001 seconds."""
    collection = db["dictionary"]
    print("Setting up database indexes...")
    # Indexing 'word' for fast exact matches
    await collection.create_index("word")
    # Optional: Index 'pos' if you ever want to filter by "Give me all nouns"
    await collection.create_index("pos")
    print("Indexes created successfully.")


def extract_word_data(raw_entry):
    """
    Extracts only necessary data for the Knowledge Graph.
    """
    word = raw_entry.get("word")
    pos = raw_entry.get("pos")

    # Skip entries that aren't useful words (like phrases or symbols)
    if not word or " " in word:
        return None

    # 1. Extract Definitions
    senses = []
    for sense in raw_entry.get("senses", []):
        # We prefer simple glosses
        if "glosses" in sense:
            senses.extend(sense["glosses"])

    # 2. Extract Gender
    gender = None
    first_sense_tags = raw_entry.get("senses", [{}])[0].get("tags", [])
    if "masculine" in first_sense_tags:
        gender = "der"
    elif "feminine" in first_sense_tags:
        gender = "die"
    elif "neuter" in first_sense_tags:
        gender = "das"

    # 3. Extract Plural Forms
    plurals = []
    if pos == "noun":
        for form in raw_entry.get("forms", []):
            if "plural" in form.get("tags", []):
                plurals.append(form.get("form"))

    return {
        "word": word,
        "pos": pos,
        "gender": gender,
        "plurals": list(set(plurals)),
        "definitions": senses[:3]
    }


async def ingest_data():
    # --- 1. CONNECT USING YOUR EXISTING LOGIC ---
    await connect_to_mongo()
    db = get_db()  # This now holds the connected database object

    if db is None:
        print("Failed to connect to DB. Check your .env settings.")
        return

    collection = db["dictionary"]

    # Clear existing data so we don't have duplicates
    print("Clearing old dictionary data...")
    await collection.delete_many({})

    await setup_indexes(db)

    print(f"Starting ingestion from {FILE_PATH}...")
    start_time = time.time()

    batch = []
    total_inserted = 0
    skipped = 0

    if not os.path.exists(FILE_PATH):
        print(f"Error: File {FILE_PATH} not found.")
        await close_mongo_connection()
        return

    # Read line-by-line
    with open(FILE_PATH, 'r', encoding='utf-8') as f:
        for line_number, line in enumerate(f, 1):
            try:
                raw_entry = json.loads(line)

                # Filter useful types
                if raw_entry.get("pos") not in ["noun", "verb", "adj", "adv"]:
                    skipped += 1
                    continue

                clean_data = extract_word_data(raw_entry)

                if clean_data:
                    batch.append(clean_data)
                else:
                    skipped += 1

                # Execute Bulk Insert
                if len(batch) >= BATCH_SIZE:
                    await collection.insert_many(batch)
                    total_inserted += len(batch)
                    batch = []
                    print(f"Inserted {total_inserted} words... (Line {line_number})")

            except Exception as e:
                print(f"Error parsing line {line_number}: {e}")
                continue

    # Insert remaining words
    if batch:
        await collection.insert_many(batch)
        total_inserted += len(batch)

    end_time = time.time()
    print("--------------------------------------------------")
    print("INGESTION COMPLETE!")
    print(f"Total words inserted: {total_inserted}")
    print(f"Time taken: {round(end_time - start_time, 2)} seconds")

    # --- 2. CLOSE CONNECTION ---
    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(ingest_data())