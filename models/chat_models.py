# models.py (Updated for Pydantic V2)
from pydantic import BaseModel, EmailStr, Field
from pydantic_core import core_schema, PydanticCustomError
from typing import Any, Optional
from bson import ObjectId
from typing import Optional
from datetime import datetime, timezone

# --- MongoDB Helper for Pydantic V2 ---

# Custom class to handle MongoDB's ObjectId when serializing
class PyObjectId(ObjectId):
    """
    Custom type for MongoDB ObjectId integration with Pydantic V2.
    It ensures that ObjectId fields are handled as strings in JSON/API,
    but can be validated from either ObjectId or string format.
    """

    @classmethod
    def __get_pydantic_core_schema__(
            cls, source_type: Any, handler: Any
    ) -> core_schema.CoreSchema:
        """
        Defines how Pydantic should validate and serialize this type.
        """

        # 1. Validation: Convert input string/ObjectId to ObjectId instance
        object_id_schema = core_schema.chain_schema(
            [
                # Check if the input is a valid ObjectId string
                core_schema.str_schema(),
                core_schema.no_info_plain_validator_function(cls.validate),
            ]
        )

        # 2. Serialization: Convert ObjectId instance to a string for output
        return core_schema.json_or_python_schema(
            json_schema=core_schema.str_schema(),
            python_schema=object_id_schema,
            serialization=core_schema.to_string_ser_schema(),  # Important: converts ObjectId to str
        )

    @classmethod
    def validate(cls, v: Any) -> ObjectId:
        if isinstance(v, ObjectId):
            return v
        if isinstance(v, str):
            if ObjectId.is_valid(v):
                return ObjectId(v)
            else:
                raise PydanticCustomError('invalid_object_id', 'Invalid ObjectId string')
        raise PydanticCustomError('invalid_type', 'ObjectId must be a string or ObjectId instance')


# --- User Schemas ---

class UserInDB(BaseModel):
    # Use PyObjectId here
    id: PyObjectId = Field(default_factory=PyObjectId, alias="_id")
    username: str = Field(..., min_length=3, max_length=20)
    email: EmailStr
    hashed_password: str
    friends: list[str] = Field(default_factory=list)

    class Config:
        # Renamed in V2: 'allow_population_by_field_name' -> 'validate_by_name'
        validate_by_name = True

        # json_encoders is also deprecated/removed in favor of the custom type's schema definition
        # We don't need json_encoders anymore because PyObjectId handles it above.
        # However, to explicitly handle any other remaining ObjectIds if present:
        # You'll use the 'ser_json_updates' argument in your Pydantic model's config
        # but for simple cases like this, the PyObjectId implementation is enough.
        pass  # Remove the old json_encoders line


# models.py (Ensure these are present at the end of the file)

# 2. Schema for user registration (incoming data)
class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    email: EmailStr
    password: str = Field(..., min_length=8)


# 3. Schema for the public profile (outgoing data, safe to share)
class UserProfile(BaseModel):
    username: str
    email: EmailStr
    bio: Optional[str] = "No bio yet."

# 4. Schema for Login Input
class UserLogin(BaseModel):
    username: str
    password: str

# 5. Schema for the Token Response
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


# 6. Schema for a Friend Request
class FriendRequestInDB(BaseModel):
    sender: str
    receiver: str
    status: str = "pending"  # pending, accepted, rejected
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
# 7. Schema for displaying a Friend Request to the user
class FriendRequestOut(BaseModel):
    id: str = Field(alias="_id")
    sender: str
    timestamp: datetime

# 8. How the message looks in the Database
class MessageInDB(BaseModel):
    conversation_id: str
    sender: str
    content: str
    timestamp: datetime

# 9. How the message looks when sent to the Frontend
class MessageOut(BaseModel):
    sender: str
    content: str
    timestamp: datetime

# 10. Create conversation via request
class CreateConversationRequest(BaseModel):
    participants: list[str] # ["alice", "bob"] for DM, or ["alice", "bob", "charlie"] for Group
    is_group: bool = False
    group_name: str | None = None

#11. How each conv is sent to client for the inbox display
class ConversationSummary(BaseModel):
    id: str
    participants: list[str]
    admins: list[str]
    type: str
    name: str
    created_at: datetime
    last_message_preview: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int = 0


class AnkiNote(BaseModel):
    id : str
    front : str
    back: str
    mod: int
    is_reviewed: bool = False


class AnkiDeckNotes(BaseModel):
    deck_name: str
    notes: list[AnkiNote]
    language: Optional[str] = "en"

class UpdateLangSchema(BaseModel):
    deck_name: str
    language: str
