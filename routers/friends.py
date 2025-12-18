# backend/routers/friends.py
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.database import Database as PyMongoDatabase
from database import get_db
from security import get_current_user  # We will create this dependency next
from models import FriendRequestInDB, UserProfile
from datetime import datetime

router = APIRouter(prefix="/friends", tags=["Friends"])


# 1. Send Friend Request
@router.post("/request/{username}")
async def send_friend_request(
        username: str,
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    target_user = username

    # Validation 1: Can't add yourself
    if current_user == target_user:
        raise HTTPException(status_code=400, detail="You cannot friend yourself.")

    # Validation 2: Target user must exist
    target_exists = await db.users.find_one({"username": target_user})
    if not target_exists:
        raise HTTPException(status_code=404, detail="User not found.")

    # Validation 3: Check if already friends
    user_doc = await db.users.find_one({"username": current_user})
    if target_user in user_doc.get("friends", []):
        raise HTTPException(status_code=400, detail="You are already friends.")

    # Validation 4: Check if request already pending
    existing_request = await db.friend_requests.find_one({
        "sender": current_user,
        "receiver": target_user,
        "status": "pending"
    })
    if existing_request:
        raise HTTPException(status_code=400, detail="Request already sent.")

    # Create Request
    new_request = FriendRequestInDB(sender=current_user, receiver=target_user)
    await db.friend_requests.insert_one(new_request.model_dump())

    return {"message": "Friend request sent"}


# 2. Accept Friend Request
@router.post("/accept/{sender_username}")
async def accept_friend_request(
        sender_username: str,
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    # Find the pending request
    request = await db.friend_requests.find_one({
        "sender": sender_username,
        "receiver": current_user,
        "status": "pending"
    })

    if not request:
        raise HTTPException(status_code=404, detail="No pending request found.")

    # Transaction: Update both users' friend lists and delete the request
    # (In a real app, use MongoDB Transactions for safety)

    # Add Sender to Receiver's list
    await db.users.update_one(
        {"username": current_user},
        {"$addToSet": {"friends": sender_username}}
    )

    # Add Receiver to Sender's list
    await db.users.update_one(
        {"username": sender_username},
        {"$addToSet": {"friends": current_user}}
    )

    # Delete the request (or mark as accepted)
    await db.friend_requests.delete_one({"_id": request["_id"]})

    return {"message": f"You are now friends with {sender_username}"}


# 3. List My Friends
@router.get("/list", response_model=list[str])
async def get_friends_list(
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    user = await db.users.find_one({"username": current_user})
    return user.get("friends", [])


# 4. List Incoming Requests
@router.get("/requests/incoming")
async def get_incoming_requests(
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    requests = await db.friend_requests.find(
        {"receiver": current_user, "status": "pending"}
    ).to_list(length=100)

    # Convert ObjectIds to strings for JSON
    return [
        {"sender": r["sender"], "timestamp": r["created_at"]}
        for r in requests
    ]