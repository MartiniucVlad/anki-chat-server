from pymongo import AsyncMongoClient
from config import settings

client: AsyncMongoClient | None = None
database = None


async def connect_to_mongo():
    global client, database

    print("Connecting to MongoDB...")

    client = AsyncMongoClient(
        settings.mongo_url,
        serverSelectionTimeoutMS=5000,
    )

    database = client[settings.mongo_db]

    await client.admin.command("ping")
    print("Successfully connected to MongoDB.")


async def close_mongo_connection():
    global client
    if client:
        print("Closing MongoDB connection.")
        await client.close()


def get_db():
    return database
