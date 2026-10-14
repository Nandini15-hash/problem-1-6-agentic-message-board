"""Request/response shapes (the public API contract)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PostKind = Literal["update", "milestone", "alert", "question", "handoff"]
AgentKind = Literal["agent", "skill"]
ReactionKind = Literal["endorse", "insightful"]


class AgentRegister(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    function: str = Field(min_length=1, max_length=64, description="What the agent does, e.g. 'code-review'")
    kind: AgentKind = "agent"
    description: str = Field(default="", max_length=4000)
    owner: str = Field(default="", max_length=128)
    capabilities: list[str] = Field(default_factory=list, max_length=50)
    handle: str | None = Field(default=None, max_length=64, description="Optional; derived from name if omitted")

    @field_validator("function")
    @classmethod
    def _norm_function(cls, v: str) -> str:
        return v.strip().lower().replace(" ", "-")


class AgentOut(BaseModel):
    handle: str
    name: str
    kind: str
    function: str
    description: str
    owner: str
    capabilities: list[str]
    created_at: datetime
    last_active_at: datetime | None
    post_count: int = 0


class AgentRegistered(AgentOut):
    token: str = Field(description="Bearer token for posting. Shown once.")


class PostCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    kind: PostKind = "update"
    tags: list[str] = Field(default_factory=list, max_length=20)
    metrics: dict[str, float | int | str] = Field(default_factory=dict)
    parent_id: int | None = None


class PostOut(BaseModel):
    id: int
    agent: AgentOut
    kind: str
    content: str
    tags: list[str]
    mentions: list[str]
    metrics: dict
    parent_id: int | None
    created_at: datetime
    reactions: dict[str, int] = Field(default_factory=dict)
    reply_count: int = 0


class FeedFilters(BaseModel):
    """Every way to slice the feed. Saved feeds store exactly this object."""

    q: str | None = None
    agents: list[str] = Field(default_factory=list)  # handles
    functions: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    mentions: str | None = None
    following: bool = False
    since: datetime | None = None
    include_replies: bool = False


class FeedPage(BaseModel):
    items: list[PostOut]
    next_cursor: int | None


class SavedFeedIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    filters: FeedFilters


class SavedFeedOut(SavedFeedIn):
    id: int


class ReactionIn(BaseModel):
    reaction: ReactionKind = "endorse"


class StatsOut(BaseModel):
    agents: int
    posts: int
    functions: dict[str, int]
    top_tags: dict[str, int]
