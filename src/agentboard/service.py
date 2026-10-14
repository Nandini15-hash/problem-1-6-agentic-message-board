"""Business logic. The API layer is a thin shell around these functions."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, contains_eager

from .models import Agent, Follow, Post, Reaction, SavedFeed, utcnow
from .schemas import (
    AgentOut,
    AgentRegister,
    FeedFilters,
    PostCreate,
    PostOut,
)

MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9_-]{0,63})")
HASHTAG_RE = re.compile(r"#([a-z0-9][a-z0-9_-]{0,47})")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


# ---------- helpers ----------

def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:64] or "agent"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def norm_tag(t: str) -> str:
    return slugify(t.lstrip("#"))[:48]


def _pack(values: list[str]) -> str:
    """['a','b'] -> ',a,b,'  (portable 'contains' via LIKE '%,a,%')."""
    uniq = sorted({v for v in values if v})
    return "," + ",".join(uniq) + ("," if uniq else "")


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; everything we store is UTC, so re-attach it."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _unpack(packed: str) -> list[str]:
    return [v for v in (packed or "").split(",") if v]


# ---------- agents ----------

def register_agent(db: Session, data: AgentRegister) -> tuple[Agent, str]:
    base = slugify(data.handle or data.name)
    handle = base
    n = 2
    while db.scalar(select(Agent.id).where(Agent.handle == handle)) is not None:
        if data.handle:  # explicit handle requested and taken
            raise Conflict(f"handle '{handle}' is taken")
        handle = f"{base}-{n}"
        n += 1
    token = "ab_" + secrets.token_urlsafe(24)
    agent = Agent(
        handle=handle,
        name=data.name,
        kind=data.kind,
        function=data.function,
        description=data.description,
        owner=data.owner,
        capabilities=data.capabilities,
        token_hash=hash_token(token),
    )
    db.add(agent)
    db.commit()
    return agent, token


def upsert_agent_from_manifest(db: Session, data: AgentRegister) -> Agent:
    """Idempotent registration used by manifest loading (no token returned)."""
    handle = slugify(data.handle or data.name)
    agent = db.scalar(select(Agent).where(Agent.handle == handle))
    if agent is None:
        agent, _ = register_agent(db, data.model_copy(update={"handle": handle}))
        return agent
    for field in ("name", "kind", "function", "description", "owner", "capabilities"):
        setattr(agent, field, getattr(data, field))
    db.commit()
    return agent


def rotate_token(db: Session, handle: str) -> str:
    agent = get_agent(db, handle)
    token = "ab_" + secrets.token_urlsafe(24)
    agent.token_hash = hash_token(token)
    db.commit()
    return token


def authenticate(db: Session, token: str) -> Agent | None:
    if not token:
        return None
    return db.scalar(select(Agent).where(Agent.token_hash == hash_token(token)))


def get_agent(db: Session, handle: str) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.handle == handle))
    if agent is None:
        raise NotFound(f"agent '{handle}' not found")
    return agent


def list_agents(db: Session, q: str | None = None, function: str | None = None,
                kind: str | None = None, limit: int = 100, offset: int = 0) -> list[Agent]:
    stmt = select(Agent)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(Agent.name).like(like), Agent.handle.like(like),
                              func.lower(Agent.description).like(like), Agent.function.like(like)))
    if function:
        stmt = stmt.where(Agent.function == function)
    if kind:
        stmt = stmt.where(Agent.kind == kind)
    stmt = stmt.order_by(Agent.last_active_at.desc().nulls_last(), Agent.id).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def post_counts(db: Session, agent_ids: list[int]) -> dict[int, int]:
    if not agent_ids:
        return {}
    rows = db.execute(select(Post.agent_id, func.count()).where(Post.agent_id.in_(agent_ids))
                      .group_by(Post.agent_id))
    return dict(rows.all())


def agent_out(agent: Agent, post_count: int = 0) -> AgentOut:
    return AgentOut(handle=agent.handle, name=agent.name, kind=agent.kind, function=agent.function,
                    description=agent.description, owner=agent.owner,
                    capabilities=agent.capabilities or [], created_at=_aware(agent.created_at),
                    last_active_at=_aware(agent.last_active_at), post_count=post_count)


# ---------- posts ----------

def create_post(db: Session, agent: Agent, data: PostCreate) -> Post:
    if data.parent_id is not None and db.get(Post, data.parent_id) is None:
        raise NotFound(f"parent post {data.parent_id} not found")
    text = data.content.lower()
    tags = [norm_tag(t) for t in data.tags] + HASHTAG_RE.findall(text)
    # Only keep mentions of agents that actually exist (no dangling @'s).
    raw_mentions = set(MENTION_RE.findall(text))
    mentions = list(db.scalars(select(Agent.handle).where(Agent.handle.in_(raw_mentions)))) if raw_mentions else []
    post = Post(agent_id=agent.id, kind=data.kind, content=data.content, tags=_pack(tags),
                mentions=_pack(mentions), metrics=data.metrics, parent_id=data.parent_id)
    agent.last_active_at = utcnow()
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def get_post(db: Session, post_id: int) -> Post:
    post = db.get(Post, post_id)
    if post is None:
        raise NotFound(f"post {post_id} not found")
    return post


def react(db: Session, post_id: int, viewer: str, reaction: str) -> None:
    get_post(db, post_id)
    exists = db.scalar(select(Reaction.id).where(Reaction.post_id == post_id, Reaction.viewer == viewer,
                                                 Reaction.reaction == reaction))
    if exists is None:
        db.add(Reaction(post_id=post_id, viewer=viewer, reaction=reaction))
        db.commit()


def _feed_stmt(db: Session, f: FeedFilters, viewer: str | None):
    # contains_eager: reuse the filter join to load the author (one join, not two).
    stmt = select(Post).join(Agent, Post.agent_id == Agent.id).options(contains_eager(Post.agent))
    conds = []
    if not f.include_replies:
        conds.append(Post.parent_id.is_(None))
    if f.q:
        # Every term must match somewhere (content, tags, agent name/handle/function).
        for term in f.q.lower().split():
            if term.startswith("#"):
                conds.append(Post.tags.like(f"%,{norm_tag(term)},%"))
                continue
            if term.startswith("@"):
                conds.append(or_(Agent.handle == term[1:], Post.mentions.like(f"%,{term[1:]},%")))
                continue
            like = f"%{term}%"
            conds.append(or_(func.lower(Post.content).like(like), Post.tags.like(like),
                             func.lower(Agent.name).like(like), Agent.handle.like(like),
                             Agent.function.like(like)))
    if f.agents:
        conds.append(Agent.handle.in_(f.agents))
    if f.functions:
        conds.append(Agent.function.in_(f.functions))
    if f.kinds:
        conds.append(Post.kind.in_(f.kinds))
    if f.tags:
        conds.append(or_(*[Post.tags.like(f"%,{norm_tag(t)},%") for t in f.tags]))
    if f.mentions:
        conds.append(Post.mentions.like(f"%,{f.mentions},%"))
    if f.since:
        conds.append(Post.created_at >= f.since)
    if f.following and viewer:
        followed = select(Follow.agent_id).where(Follow.viewer == viewer)
        conds.append(Post.agent_id.in_(followed))
    if conds:
        stmt = stmt.where(and_(*conds))
    return stmt


def query_feed(db: Session, f: FeedFilters, viewer: str | None = None, limit: int = 30,
               cursor: int | None = None, after_id: int | None = None) -> list[Post]:
    """Keyset pagination on post id: O(limit) per page no matter how big the table gets."""
    stmt = _feed_stmt(db, f, viewer)
    if cursor is not None:
        stmt = stmt.where(Post.id < cursor)
    if after_id is not None:
        stmt = stmt.where(Post.id > after_id).order_by(Post.id.asc())
    else:
        stmt = stmt.order_by(Post.id.desc())
    return list(db.scalars(stmt.limit(limit)).unique())


def posts_out(db: Session, posts: list[Post]) -> list[PostOut]:
    ids = [p.id for p in posts]
    reactions: dict[int, dict[str, int]] = {}
    replies: dict[int, int] = {}
    if ids:
        for pid, kind, n in db.execute(select(Reaction.post_id, Reaction.reaction, func.count())
                                       .where(Reaction.post_id.in_(ids))
                                       .group_by(Reaction.post_id, Reaction.reaction)):
            reactions.setdefault(pid, {})[kind] = n
        replies = dict(db.execute(select(Post.parent_id, func.count()).where(Post.parent_id.in_(ids))
                                  .group_by(Post.parent_id)).all())
    return [PostOut(id=p.id, agent=agent_out(p.agent), kind=p.kind, content=p.content,
                    tags=_unpack(p.tags), mentions=_unpack(p.mentions), metrics=p.metrics or {},
                    parent_id=p.parent_id, created_at=_aware(p.created_at),
                    reactions=reactions.get(p.id, {}), reply_count=replies.get(p.id, 0))
            for p in posts]


def replies_of(db: Session, post_id: int) -> list[Post]:
    get_post(db, post_id)
    return list(db.scalars(select(Post).where(Post.parent_id == post_id).order_by(Post.id)).unique())


# ---------- follows & saved feeds ----------

def follow(db: Session, viewer: str, handle: str) -> None:
    agent = get_agent(db, handle)
    if db.scalar(select(Follow.id).where(Follow.viewer == viewer, Follow.agent_id == agent.id)) is None:
        db.add(Follow(viewer=viewer, agent_id=agent.id))
        db.commit()


def unfollow(db: Session, viewer: str, handle: str) -> None:
    agent = get_agent(db, handle)
    row = db.scalar(select(Follow).where(Follow.viewer == viewer, Follow.agent_id == agent.id))
    if row:
        db.delete(row)
        db.commit()


def following(db: Session, viewer: str) -> list[str]:
    return list(db.scalars(select(Agent.handle).join(Follow, Follow.agent_id == Agent.id)
                           .where(Follow.viewer == viewer).order_by(Agent.handle)))


def save_feed(db: Session, viewer: str, name: str, filters: FeedFilters) -> SavedFeed:
    row = db.scalar(select(SavedFeed).where(SavedFeed.viewer == viewer, SavedFeed.name == name))
    payload = filters.model_dump(mode="json")
    if row:
        row.filters = payload
    else:
        row = SavedFeed(viewer=viewer, name=name, filters=payload)
        db.add(row)
    db.commit()
    return row


def list_saved_feeds(db: Session, viewer: str) -> list[SavedFeed]:
    return list(db.scalars(select(SavedFeed).where(SavedFeed.viewer == viewer).order_by(SavedFeed.name)))


def get_saved_feed(db: Session, viewer: str, feed_id: int) -> SavedFeed:
    row = db.get(SavedFeed, feed_id)
    if row is None or row.viewer != viewer:
        raise NotFound(f"saved feed {feed_id} not found")
    return row


def delete_saved_feed(db: Session, viewer: str, feed_id: int) -> None:
    db.delete(get_saved_feed(db, viewer, feed_id))
    db.commit()


# ---------- stats ----------

def stats(db: Session, since: datetime | None = None) -> dict:
    n_agents = db.scalar(select(func.count()).select_from(Agent)) or 0
    n_posts = db.scalar(select(func.count()).select_from(Post)) or 0
    functions = dict(db.execute(select(Agent.function, func.count()).group_by(Agent.function)).all())
    tag_stmt = select(Post.tags).order_by(Post.id.desc()).limit(2000)  # recent window keeps it cheap
    if since:
        tag_stmt = tag_stmt.where(Post.created_at >= since)
    counter: Counter[str] = Counter()
    for packed in db.scalars(tag_stmt):
        counter.update(_unpack(packed))
    return {"agents": n_agents, "posts": n_posts, "functions": functions,
            "top_tags": dict(counter.most_common(15))}
