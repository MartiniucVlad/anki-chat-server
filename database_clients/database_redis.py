# backend/database_redis.py
import redis.asyncio as redis
import os

# "redis://localhost:6379" connects to the docker container you just started
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# We use decode_responses=True so we get "strings" back instead of byte code (b'string')
redis_client = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

async def get_redis():
    return redis_client