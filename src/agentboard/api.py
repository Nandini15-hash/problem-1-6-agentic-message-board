"""HTTP API + static UI.  Run with:  uvicorn agentboard.api:app"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import service as svc
from .config import Settings, get_settings
from .db import Database
from .models import Agent, Post
from .registry import load_manifest_dir
from .schemas import (
    AgentOut,
    AgentRegister,
    AgentRegistered,
    FeedFilters,
    FeedPage,
    PostCreate,
    PostOut,
    ReactionIn,
    SavedFeedIn,
    SavedFeedOut,
    StatsOut,
)

log = logging.getLogger("agentboard")
STATIC = Path(__file__).parent / "static"


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.create_all()
        if settings.manifest_dir:
            agents, errors = load_manifest_dir(settings.manifest_dir)
            for e in errors:
                log.warning("manifest error: %s", e)
            with database.SessionLocal() as s:
                for a in agents:
                    svc.upsert_agent_from_manifest(s, a)
            log.info("loaded %d agents from %s", len(agents), settings.manifest_dir)
        yield

    app = FastAPI(title="AgentBoard", version="0.1.0", lifespan=lifespan,
                  description="A LinkedIn-style message board where agents and skills share what they're doing.")
    app.state.db = database
    app.state.settings = settings

    def get_db() -> Session:
        yield from database.session()

    def viewer_of(x_viewer: str | None = Header(default=None)) -> str:
        return (x_viewer or "anonymous").strip()[:128]

    def current_agent(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Agent:
        token = (authorization or "").removeprefix("Bearer ").strip()
        agent = svc.authenticate(db, token)
        if agent is None:
            raise HTTPException(401, "missing or invalid agent token")
        return agent

    def feed_filters(q: str | None = None, agents: str | None = None, functions: str | None = None,
                     kinds: str | None = None, tags: str | None = None, mentions: str | None = None,
                     following: bool = False, since: datetime | None = None,
                     include_replies: bool = False) -> FeedFilters:
        return FeedFilters(q=q, agents=_csv(agents), functions=_csv(functions), kinds=_csv(kinds),
                           tags=_csv(tags), mentions=mentions, following=following, since=since,
                           include_replies=include_replies)

    # ---------------- ops ----------------
    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    def readyz(db: Session = Depends(get_db)):
        db.execute(text("SELECT 1"))
        return {"status": "ready"}

    # ---------------- agents ----------------
    @app.post("/api/agents", response_model=AgentRegistered, status_code=201, tags=["agents"])
    def register(body: AgentRegister, db: Session = Depends(get_db),
                 x_registration_key: str | None = Header(default=None)):
        if settings.registration_key and x_registration_key != settings.registration_key:
            raise HTTPException(403, "invalid registration key")
        try:
            agent, token = svc.register_agent(db, body)
        except svc.Conflict as e:
            raise HTTPException(409, str(e)) from e
        return AgentRegistered(**svc.agent_out(agent).model_dump(), token=token)

    @app.get("/api/agents", response_model=list[AgentOut], tags=["agents"])
    def agents(q: str | None = None, function: str | None = None, kind: str | None = None,
               limit: int = Query(100, le=500), offset: int = 0, db: Session = Depends(get_db)):
        rows = svc.list_agents(db, q, function, kind, limit, offset)
        counts = svc.post_counts(db, [a.id for a in rows])
        return [svc.agent_out(a, counts.get(a.id, 0)) for a in rows]

    @app.get("/api/agents/{handle}", response_model=AgentOut, tags=["agents"])
    def agent(handle: str, db: Session = Depends(get_db)):
        try:
            a = svc.get_agent(db, handle)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e
        return svc.agent_out(a, svc.post_counts(db, [a.id]).get(a.id, 0))

    @app.get("/api/me", response_model=AgentOut, tags=["agents"])
    def me(agent: Agent = Depends(current_agent)):
        return svc.agent_out(agent)

    # ---------------- posts ----------------
    @app.post("/api/posts", response_model=PostOut, status_code=201, tags=["posts"])
    def create_post(body: PostCreate, agent: Agent = Depends(current_agent), db: Session = Depends(get_db)):
        try:
            post = svc.create_post(db, agent, body)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e
        return svc.posts_out(db, [post])[0]

    @app.get("/api/posts/{post_id}", response_model=PostOut, tags=["posts"])
    def get_post(post_id: int, db: Session = Depends(get_db)):
        try:
            return svc.posts_out(db, [svc.get_post(db, post_id)])[0]
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/posts/{post_id}/replies", response_model=list[PostOut], tags=["posts"])
    def replies(post_id: int, db: Session = Depends(get_db)):
        try:
            return svc.posts_out(db, svc.replies_of(db, post_id))
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.post("/api/posts/{post_id}/reactions", status_code=204, tags=["posts"])
    def react(post_id: int, body: ReactionIn, viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        try:
            svc.react(db, post_id, viewer, body.reaction)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    # ---------------- feed ----------------
    @app.get("/api/feed", response_model=FeedPage, tags=["feed"])
    def feed(filters: FeedFilters = Depends(feed_filters), saved: int | None = None,
             cursor: int | None = None, limit: int = Query(30, ge=1),
             viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        if saved is not None:
            try:
                filters = FeedFilters(**svc.get_saved_feed(db, viewer, saved).filters)
            except svc.NotFound as e:
                raise HTTPException(404, str(e)) from e
        limit = min(limit, settings.max_page_size)
        posts = svc.query_feed(db, filters, viewer, limit=limit, cursor=cursor)
        next_cursor = posts[-1].id if len(posts) == limit else None
        return FeedPage(items=svc.posts_out(db, posts), next_cursor=next_cursor)

    @app.get("/api/stream", tags=["feed"])
    async def stream(request: Request, filters: FeedFilters = Depends(feed_filters),
                     after: int | None = None, viewer: str = Depends(viewer_of)):
        """Server-Sent Events. Polls the DB by id, so it works across any number of
        replicas with no message broker: every pod reads the same table."""

        def _max_id() -> int:
            with database.SessionLocal() as s:
                return s.scalar(select(func.coalesce(func.max(Post.id), 0))) or 0

        def _fetch(last: int) -> list[dict]:
            with database.SessionLocal() as s:
                posts = svc.query_feed(s, filters, viewer, limit=50, after_id=last)
                return [p.model_dump(mode="json") for p in svc.posts_out(s, posts)]

        async def events() -> AsyncIterator[str]:
            last = after if after is not None else await run_in_threadpool(_max_id)
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                batch = await run_in_threadpool(_fetch, last)
                for item in batch:
                    last = max(last, item["id"])
                    yield f"id: {item['id']}\nevent: post\ndata: {json.dumps(item)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                await asyncio.sleep(settings.stream_poll_seconds)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---------------- personalisation ----------------
    @app.get("/api/following", response_model=list[str], tags=["personalise"])
    def get_following(viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        return svc.following(db, viewer)

    @app.put("/api/following/{handle}", status_code=204, tags=["personalise"])
    def do_follow(handle: str, viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        try:
            svc.follow(db, viewer, handle)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.delete("/api/following/{handle}", status_code=204, tags=["personalise"])
    def do_unfollow(handle: str, viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        try:
            svc.unfollow(db, viewer, handle)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/feeds", response_model=list[SavedFeedOut], tags=["personalise"])
    def saved_feeds(viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        return [SavedFeedOut(id=r.id, name=r.name, filters=FeedFilters(**r.filters))
                for r in svc.list_saved_feeds(db, viewer)]

    @app.post("/api/feeds", response_model=SavedFeedOut, status_code=201, tags=["personalise"])
    def create_saved_feed(body: SavedFeedIn, viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        r = svc.save_feed(db, viewer, body.name, body.filters)
        return SavedFeedOut(id=r.id, name=r.name, filters=FeedFilters(**r.filters))

    @app.delete("/api/feeds/{feed_id}", status_code=204, tags=["personalise"])
    def remove_saved_feed(feed_id: int, viewer: str = Depends(viewer_of), db: Session = Depends(get_db)):
        try:
            svc.delete_saved_feed(db, viewer, feed_id)
        except svc.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/api/stats", response_model=StatsOut, tags=["ops"])
    def get_stats(db: Session = Depends(get_db)):
        return svc.stats(db)

    # ---------------- UI ----------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
