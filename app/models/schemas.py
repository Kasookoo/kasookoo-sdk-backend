from pydantic import BaseModel
from typing import Any, Dict, List, Optional

class Message(BaseModel):
    message: str

class Token(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str
    created_at: str
    expiry_minutes: int