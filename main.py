# main.py (NEW/RECOMMENDED)
import contextlib
from fastapi import FastAPI
from database import connect_to_mongo, close_mongo_connection
from routers.users import router as users_router


# --- 1. Define the Lifespan Context Manager ---

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    print("Application startup: Connecting DB...")
    await connect_to_mongo()

    # The 'yield' pauses execution, and the application starts serving requests.
    yield

    # --- Shutdown Logic (Executed after the application stops serving) ---
    print("Application shutdown: Closing DB connection...")
    await close_mongo_connection()


# --- 2. Pass the lifespan function to FastAPI ---
app = FastAPI(
    title="Mega App Backend",
    description="User profiles, chat, and interactive games.",
    # IMPORTANT: Pass the new lifespan function here
    lifespan=lifespan
)

# --- Routers ---
app.include_router(users_router)


@app.get("/", tags=["Root"])
async def root():
    return {"message": "Welcome to the Mega App API! Go to /docs for more info."}