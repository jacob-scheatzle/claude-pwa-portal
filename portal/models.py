from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="user")  # "admin" or "user"
    created_at: datetime = Field(default_factory=_utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class App(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str
    description: Optional[str] = None
    version: str
    icon: Optional[str] = None  # relative path inside the app dir
    entry: str = "index.html"
    services: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    enabled: bool = Field(default=True)
    uploaded_by: Optional[int] = Field(default=None, foreign_key="user.id")
    uploaded_at: datetime = Field(default_factory=_utcnow)


class ApiToken(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=80)
    token_hash: str = Field(index=True)
    prefix: str  # first 8 chars of the raw token, shown for identification
    created_by: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_utcnow)
    last_used_at: Optional[datetime] = None
