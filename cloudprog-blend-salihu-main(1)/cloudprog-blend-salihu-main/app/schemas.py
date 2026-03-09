from pydantic import BaseModel, Field


class InputRequest(BaseModel):
    input_text: str = Field(..., min_length=1)
    user_id: str | None = None


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)