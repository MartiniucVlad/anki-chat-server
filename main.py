# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware # <--- IMPORTS MUST BE HERE
from database_clients.database_mongo import connect_to_mongo, close_mongo_connection
from routers.users import router as users_router
from routers.friends import router as friends_router
from routers.chat import router as chat_router
from routers.anki import router as anki_router
from routers.websocket.ws_hub import router as ws_hub_router
from routers.stories import router as stories_router
import contextlib

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    print("Startup: Connecting to DB...")
    await connect_to_mongo()
    yield
    print("Shutdown: Closing DB...")
    await close_mongo_connection()

app = FastAPI(lifespan=lifespan)

# --- CORS SETTINGS ---

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Allows ALL origins (easiest for dev)
    allow_credentials=True,
    allow_methods=["*"],        # Allows all methods (POST, GET, OPTIONS, etc.)
    allow_headers=["*"],        # Allows all headers
)

app.include_router(users_router)
app.include_router(friends_router)
app.include_router(chat_router)
app.include_router(anki_router)
app.include_router(ws_hub_router)
app.include_router(stories_router)

@app.get("/")
async def root():
    return {"message": "Server is running"}