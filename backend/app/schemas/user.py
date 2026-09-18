import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    extra_departments: list[str] = []
    confidentiality_override: str | None = None
    extra_tools: list[str] = []
    # Role default merged with the overrides above - what the user can
    # actually do right now, for the admin UI to display without
    # recomputing the policy merge on the client.
    effective_departments: list[str] = []
    effective_max_confidentiality: str = "NONE"
    effective_tools: list[str] = []


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: str
    password: str = Field(min_length=8, max_length=255)


class UserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=255)


class UserAccessUpdate(BaseModel):
    """Director-granted exceptions on top of the role's default access."""

    extra_departments: list[str] | None = None
    confidentiality_override: str | None = Field(default=None, description="Set to null to clear the override")
    extra_tools: list[str] | None = None
