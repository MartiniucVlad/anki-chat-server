# backend/services/ai_service.py
from sentence_transformers import SentenceTransformer
import logging

logger = logging.getLogger(__name__)

# 1. Load the "Speed King" model
# This downloads about 90MB automatically the first time you run it.
model = SentenceTransformer('all-MiniLM-L6-v2')

def get_embedding(text: str) -> list[float]:

    try:
        # encode() returns a numpy array, we convert to list for JSON/Qdrant
        vector = model.encode(text)
        return vector.tolist()
    except Exception as e:
        logger.error(f"Error generating embedding: {e}")
        return []