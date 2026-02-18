"""
Pydantic schemas for Island CRUD Admin
Data validation and serialization
"""

from typing import List, Optional
from pydantic import BaseModel, Field


class IslandBase(BaseModel):
    """Base schema for Island with common fields"""
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., min_length=1, max_length=100)
    items: List[str] = Field(default_factory=list)
    theme: str = Field(..., pattern="^(pink|teal|purple|gold)$")
    cat: str = Field(..., pattern="^(public|member)$")
    description: str = Field(..., min_length=1)
    seasonal: str = Field(..., min_length=1, max_length=50)
    map_url: Optional[str] = None


class IslandCreate(IslandBase):
    """Schema for creating a new island"""
    id: str = Field(..., min_length=1, max_length=50, pattern="^[a-z0-9_-]+$")


class IslandUpdate(IslandBase):
    """Schema for updating an existing island"""
    pass


class IslandResponse(IslandBase):
    """Schema for island response"""
    id: str

    class Config:
        from_attributes = True


class Token(BaseModel):
    """JWT token response"""
    access_token: str
    token_type: str


class LoginRequest(BaseModel):
    """Login request payload"""
    username: str
    password: str
