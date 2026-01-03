# backend/services/search_service.py
import uuid
from datetime import datetime
from qdrant_client.models import PointStruct, VectorParams, Distance
from database_clients.database_qdrant import get_qdrant
from message_handling.messages_embeddings import get_embedding

# We use a constant collection name
COLLECTION_NAME = "messages"

def ensure_collection_exists():
    """
    Checks if the 'messages' collection exists in Qdrant.
    If not, it creates it with the correct vector size (384 for MiniLM).
    """
    client = get_qdrant()
    collections = client.get_collections().collections
    exists = any(c.name == COLLECTION_NAME for c in collections)

    if not exists:
        print(f"Creating Qdrant collection: {COLLECTION_NAME}")
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE)
        )

async def index_message(
    mongo_id: str,
    content: str,
    conversation_id: str,
    sender: str,
    timestamp: datetime
):
    """
    Generates an embedding and saves the message to Qdrant.
    """
    client = get_qdrant()
    # 1. Generate Vector (The AI part)
    # This runs locally on your CPU using ai_service.py
    vector = get_embedding(content)
    if not vector:
        print("Failed to generate embedding, skipping index.")
        return

    # 2. Upsert to Qdrant
    # Qdrant requires a UUID or Integer for the Point ID.
    # MongoDB ObjectIds are strings, so we generate a random UUID for Qdrant
    # and store the real Mongo ID in the payload.
    point_id = str(uuid.uuid4())

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=point_id,
                vector=vector,
                payload={
                    "mongo_id": mongo_id,
                    "content": content,
                    "conversation_id": conversation_id,
                    "sender": sender,
                    "timestamp": timestamp.isoformat()
                }
            )
        ]
    )
    print(f"Indexed message {mongo_id} in Qdrant.")

async def search_similar_messages(query: str, limit: int = 5, conversation_id: str = None):
    """
    1. Embeds the user query.
    2. Searches Qdrant for the nearest vectors.
    3. Returns the message payloads.
    """
    client = get_qdrant()

    # 1. Convert query to vector
    query = get_embedding(query)
    if not query:
        return []

    # 2. Define Filters (Optional: Search only inside one chat)
    query_filter = None
    if conversation_id:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        query_filter = Filter(
            must=[
                FieldCondition(
                    key="conversation_id",
                    match=MatchValue(value=conversation_id)
                )
            ]
        )

    # 3. Perform Search
    search_result = client.query_points(
        collection_name=COLLECTION_NAME,
        query=query,
        limit=limit,
        query_filter=query_filter
    ).points  # Note: We access .points at the end

    # 4. Extract payloads (clean up the response)
    results = []
    for hit in search_result:
        results.append({
            "content": hit.payload.get("content"),
            "sender": hit.payload.get("sender"),
            "timestamp": hit.payload.get("timestamp"),
            "score": hit.score  # How confident the AI is (0 to 1)
        })

    return results