import json
import asyncio
import time
import os


from database_clients.database_mongo import connect_to_mongo, close_mongo_connection, get_db
from config import settings

FILE_PATH = "z_ingest_synonyms/openthesaurus.txt"
BATCH_SIZE = 5000


async def ingest():
    await connect_to_mongo()
    db = get_db()
    await db.synonyms.drop()
    await db.synonyms.create_index("words")  # index the array field

    docs = []

    with open(FILE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            pos = None
            if line.startswith("("):
                end = line.index(")")
                pos = line[1:end].strip()
                line = line[end+1:].strip()

            words = [w.strip() for w in line.split(";") if w.strip()]
            if len(words) < 2:
                continue

            docs.append({
                "words": words,  # the full group
                "pos": pos,
            })

            if len(docs) >= 1000:
                await db.synonyms.insert_many(docs)
                docs.clear()

    if docs:
        await db.synonyms.insert_many(docs)

    count = await db.synonyms.count_documents({})
    print(f"Done. {count} groups inserted.")


if __name__ == "__main__":
    asyncio.run(ingest())