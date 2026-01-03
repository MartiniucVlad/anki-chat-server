# backend/database_qdrant.py
from qdrant_client import QdrantClient
import os

# Since you are running code in PyCharm (Host) and Qdrant in Docker:
# We connect to "localhost" on port 6333.
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", 6333))

# Initialize the client
# This client handles all the HTTP requests to the vector database
client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)

def get_qdrant():
    """
    Returns the initialized Qdrant client instance.
    Use this function in your services to interact with the vector DB.
    """
    return client