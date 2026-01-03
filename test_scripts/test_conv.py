# scripts/generate_test_data.py
import asyncio
import sys
import os
from datetime import datetime, timedelta
from bson import ObjectId

# Setup paths to import backend modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from database_clients.database_mongo import connect_to_mongo, close_mongo_connection, get_db
from message_handling.search_service import index_message, ensure_collection_exists

# --- CONFIGURATION ---
TARGET_CONVERSATION_ID = "6953b10eee783741b2ae5fca"  # <--- YOUR GROUP ID
USER_A = "relu1"
USER_B = "relu2"


async def generate_conversation():
    print(f"⏳ Connecting to DB to populate chat {TARGET_CONVERSATION_ID}...")
    await connect_to_mongo()
    ensure_collection_exists()
    db = get_db()

    # 1. Verify the conversation exists
    print("🔍 Verifying conversation exists...")
    conv = await db.conversations.find_one({"_id": ObjectId(TARGET_CONVERSATION_ID)})

    if not conv:
        print(f"❌ Error: Conversation {TARGET_CONVERSATION_ID} not found in MongoDB!")
        await close_mongo_connection()
        return

    print(f"✅ Found conversation: '{conv.get('name', 'Unknown')}' with {conv.get('participants')}")

    # 2. Define the Dialogue (3 Distinct Topics)
    dialogue = [
        # --- TOPIC 1: FOOD ---
        (USER_A, "Hey Relu, are you hungry yet?"),
        (USER_B, "Starving. I haven't eaten since breakfast."),
        (USER_A, "I was thinking we could grab some Italian food tonight."),
        (USER_B, "Oh I love pasta. There is a new place on Main St."),
        (USER_A, "Perfect, let's get some carbonara."),

        # --- TOPIC 2: CODING ---
        (USER_B, "By the way, did you fix that bug in the backend?"),
        (USER_A, "Not yet, the server keeps crashing on startup."),
        (USER_B, "It might be a Docker issue. Check the logs."),
        (USER_A, "Good idea. I think the environment variables are missing."),
        (USER_B, "Yeah, try adding the .env file to the container."),

        # --- TOPIC 3: TRAVEL ---
        (USER_A, "Enough work talk. Did you book the tickets for Spain?"),
        (USER_B, "I'm looking at flights right now. Prices are high."),
        (USER_A, "We need to go to the beach this summer."),
        (USER_B, "I found a hotel near the coast. It looks sunny."),
        (USER_A, "Book it! I need a vacation.")
    ]

    # 3. Insert Messages
    print("🚀 Inserting messages...")

    # Start time: 3 hours ago, spaced out by 5 minutes
    start_time = datetime.now() - timedelta(hours=3)

    for i, (sender, content) in enumerate(dialogue):
        msg_time = start_time + timedelta(minutes=i * 5)

        # A. Insert into MongoDB
        res = await db.messages.insert_one({
            "conversation_id": ObjectId(TARGET_CONVERSATION_ID),
            "sender": sender,
            "content": content,
            "timestamp": msg_time
        })
        msg_id = str(res.inserted_id)

        # B. Index into Qdrant
        await index_message(
            mongo_id=msg_id,
            content=content,
            conversation_id=TARGET_CONVERSATION_ID,
            sender=sender,
            timestamp=msg_time
        )
        print(f"   Saved: [{sender}] {content[:30]}...")

    # 4. Update the Conversation (Last Message Preview)
    last_msg = dialogue[-1]
    await db.conversations.update_one(
        {"_id": ObjectId(TARGET_CONVERSATION_ID)},
        {
            "$set": {
                "last_message_at": start_time + timedelta(minutes=len(dialogue) * 5),
                "last_message_preview": last_msg[1][:30] + "..."
            }
        }
    )

    print("\n✅ Done! 'relu1' and 'relu2' have chatted in your group.")
    await close_mongo_connection()


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(generate_conversation())