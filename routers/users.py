# routers/users.py
from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pymongo.database import Database as PyMongoDatabase
from models import UserRegister, UserInDB, UserProfile, UserLogin, Token
from database_mongo import get_db

# NEW: Import security functions
from security import *
from datetime import timedelta

router = APIRouter(
    prefix="/users",
    tags=["Users and Profiles"],
)


# --- REGISTER (Updated with Real Hashing) ---
@router.post("/register", response_model=UserProfile, status_code=status.HTTP_201_CREATED)
async def register_user(
        user_data: UserRegister, db: PyMongoDatabase = Depends(get_db)
):
    users_collection = db.users

    # 1. Check existing user
    if await users_collection.find_one({"username": user_data.username}):
        raise HTTPException(status_code=400, detail="Username already taken")
    if await users_collection.find_one({"email": user_data.email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    # 2. Hash the password
    hashed_password = get_password_hash(user_data.password)

    # 3. Save to DB
    user_db = UserInDB(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed_password,
    )
    new_user = await users_collection.insert_one(user_db.model_dump(by_alias=True))

    created_user = await users_collection.find_one({"_id": new_user.inserted_id})
    return UserProfile(**created_user)


@router.post("/login", response_model=Token)
async def login_for_access_token(
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: PyMongoDatabase = Depends(get_db)
):
    users_collection = db.users
    user = await users_collection.find_one({"username": form_data.username})

    if not user or not verify_password(form_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 1. Create Access Token (Short lived)
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=access_token_expires
    )

    # 2. Create Refresh Token (Long lived)
    refresh_token_expires = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    refresh_token = create_refresh_token(
        data={"sub": user["username"]},
        expires_delta=refresh_token_expires
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }


# --- REFRESH TOKEN (New Endpoint) ---
@router.post("/refresh", response_model=Token)
async def refresh_token_func(
        refresh_token: str = Body(..., embed=True)  # Expects JSON: { "refresh_token": "..." }
):
    """
    Takes a valid refresh token and returns a new access token.
    """
    print("got refresh token " + refresh_token)
    # 1. Verify the refresh token
    username = await verify_refresh_token(refresh_token)

    # 2. Create new Access Token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    new_access_token = create_access_token(
        data={"sub": username},
        expires_delta=access_token_expires
    )

    # Optional: You can choose to rotate the refresh token here too if you want strict security
    # For now, we just return the original refresh token (or you can issue a new one)

    return {
        "access_token": new_access_token,
        "refresh_token": refresh_token,  # Return the same refresh token
        "token_type": "bearer"
    }
# --- PROFILE (Unchanged) ---
@router.get("/profile/{username}", response_model=UserProfile)
async def get_user_profile(
        username: str, db: PyMongoDatabase = Depends(get_db)
):
    users_collection = db.users
    user = await users_collection.find_one({"username": username})
    if user:
        return UserProfile(**user)
    else:
        raise HTTPException(status_code=404, detail="User not found")