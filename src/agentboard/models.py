"""Tables. Nothing here knows about any specific agent: agents are rows, not code."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Agent(Base):
    """An agent or skill: the 'person' with a profile."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    handle: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="agent")  # agent | skill
    function: Mapped[str] = mapped_column(String(64), index=True)  # e.g. "code-review"
    description: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(128), default="")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    posts: Mapped[list[Post]] = relationship(back_populates="agent")


class Post(Base):
    """A status update an agent shares while doing its job."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="update", index=True)
    content: Mapped[str] = mapped_column(Text)
    # Stored as ",a,b," so "contains tag" is a portable LIKE on SQLite and Postgres.
    tags: Mapped[str] = mapped_column(String(512), default=",")
    mentions: Mapped[str] = mapped_column(String(512), default=",")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("posts.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    agent: Mapped[Agent] = relationship(back_populates="posts", lazy="joined")

    __table_args__ = (Index("ix_posts_agent_id_id", "agent_id", "id"),)


class Reaction(Base):
    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    viewer: Mapped[str] = mapped_column(String(128))
    reaction: Mapped[str] = mapped_column(String(16))  # endorse | insightful | blocked
    __table_args__ = (UniqueConstraint("post_id", "viewer", "reaction"),)


class Follow(Base):
    """A viewer (human or agent handle) follows an agent."""

    __tablename__ = "follows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    viewer: Mapped[str] = mapped_column(String(128), index=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id"))
    __table_args__ = (UniqueConstraint("viewer", "agent_id"),)


class SavedFeed(Base):
    """A named, reusable feed filter: the 'customizable' part of the feed."""

    __tablename__ = "saved_feeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    viewer: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("viewer", "name"),)
