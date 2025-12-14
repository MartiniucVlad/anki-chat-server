# routers/users.py
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pymongo.database import Database as PyMongoDatabase
from models import UserRegister, UserInDB, UserProfile
from database import get_db


# Placeholder for real password hashing (USE passlib in production!)
def hash_password(password: str) -> str:
    """In a real app, use passlib's CryptContext (e.g., bcrypt)"""
    return f"FAKE_HASH_{password}"


router = APIRouter(
    prefix="/users",
    tags=["Users and Profiles"],
)


@router.post("/register", response_model=UserProfile, status_code=status.HTTP_201_CREATED)
async def register_user(
        user_data: UserRegister, db: PyMongoDatabase = Depends(get_db)
):
    """
    Register a new user account. Checks for existing username/email.
    """
    users_collection = db.users

    # 1. Check if username already exists
    if await users_collection.find_one({"username": user_data.username}):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already taken",
        )

    # 2. Check if email already exists
    if await users_collection.find_one({"email": user_data.email}):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # 3. Create the user object for the database
    hashed_password = hash_password(user_data.password)
    user_db = UserInDB(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hashed_password,
    )

    # 4. Insert into MongoDB
    new_user = await users_collection.insert_one(user_db.model_dump(by_alias=True))

    # 5. Retrieve the created document and return the public profile
    created_user = await users_collection.find_one({"_id": new_user.inserted_id})
    return UserProfile(**created_user)


@router.get("/profile/{username}", response_model=UserProfile)
async def get_user_profile(
        username: str, db: PyMongoDatabase = Depends(get_db)
):
    """
    Retrieve the public profile of a user by username.
    """
    users_collection = db.users
    user = await users_collection.find_one({"username": username})

    if user:
        return UserProfile(**user)
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )