from pydantic import BaseModel
from typing import Dict, Any, List

class Message(BaseModel):
    message: str

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    created_at: str
    expiry_minutes: int