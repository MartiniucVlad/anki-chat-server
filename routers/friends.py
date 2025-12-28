# backend/routers/friends.py
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.database import Database as PyMongoDatabase
from database_mongo import get_db
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
from fastapi import Body

@router.post("/respond/{sender_username}")
async def respond_friend_request(
    sender_username: str,
    action: str = Body(..., embed=True),  # expects { "action": "accept" | "reject" }
    current_user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db)
):
    # 1. Find pending request
    request = await db.friend_requests.find_one({
        "sender": sender_username,
        "receiver": current_user,
        "status": "pending"
    })

    if not request:
        raise HTTPException(status_code=404, detail="No pending request found.")

    # 2. Handle ACCEPT
    if action == "accept":
        # Add each user to the other's friend list
        await db.users.update_one(
            {"username": current_user},
            {"$addToSet": {"friends": sender_username}}
        )

        await db.users.update_one(
            {"username": sender_username},
            {"$addToSet": {"friends": current_user}}
        )

        # Remove the request
        await db.friend_requests.delete_one({"_id": request["_id"]})

        return {"message": f"You are now friends with {sender_username} 🎉"}

    # 3. Handle REJECT
    elif action == "reject":
        await db.friend_requests.delete_one({"_id": request["_id"]})

        return {"message": f"Friend request from {sender_username} rejected ❌"}

    # 4. Invalid action
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid action. Use 'accept' or 'reject'."
        )



# 3. List My Friends
@router.get("/list", response_model=list[str])
async def get_friends_list(
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    user = await db.users.find_one({"username": current_user})
    return user.get("friends", [])


@router.get("/no-conversation", response_model=list[str])
async def get_friends_without_conversation(
        current_user: str = Depends(get_current_user),
        db: PyMongoDatabase = Depends(get_db)
):
    # 1. Get the current user's friend list
    user = await db.users.find_one({"username": current_user})
    if not user:
        return []

    all_friends = user.get("friends", [])
    if not all_friends:
        return []

    # 2. Find existing private conversations involving the current user
    # Optimization: Use projection {"participants": 1} to only fetch the array, not the whole chat
    cursor = db.conversations.find(
        {
            "type": "private",
            "participants": current_user
        },
        {"participants": 1}
    )

    existing_conversations = await cursor.to_list(length=None)

    # 3. Extract the "other" person from each conversation into a Set
    # We use a set for O(1) lookup speed
    users_with_chat = set()
    for conv in existing_conversations:
        for participant in conv["participants"]:
            if participant != current_user:
                users_with_chat.add(participant)

    # 4. Filter: Return friends who are NOT in the active chat set
    friends_without_chat = [
        friend for friend in all_friends
        if friend not in users_with_chat
    ]

    return friends_without_chat



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


@router.delete("/{friend_username}")
async def unfriend_user(
    friend_username: str,
    current_user: str = Depends(get_current_user),
    db: PyMongoDatabase = Depends(get_db)
):
    # 1. Prevent self-unfriend
    if friend_username == current_user:
        raise HTTPException(
            status_code=400,
            detail="You cannot unfriend yourself."
        )

    # 2. Check if they are actually friends
    user = await db.users.find_one(
        {"username": current_user, "friends": friend_username}
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"You are not friends with {friend_username}."
        )

    # 3. Remove each other from friends lists
    await db.users.update_one(
        {"username": current_user},
        {"$pull": {"friends": friend_username}}
    )

    await db.users.update_one(
        {"username": friend_username},
        {"$pull": {"friends": current_user}}
    )

    return {
        "message": f"You are no longer friends with {friend_username}."
    }



