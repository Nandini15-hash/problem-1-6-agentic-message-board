"""Database engine and session factory. Works with SQLite and Postgres."""

from __future__ import annotations

from collections.abc import Iterator

import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


class Database:
    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "postgresql":
            self._postgres_search_indexes()

    def _postgres_search_indexes(self) -> None:
        """Trigram GIN indexes make substring search (ILIKE '%x%') index-backed on Postgres.
        Best effort: skipped if the role can't create extensions."""
        stmts = [
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            "CREATE INDEX IF NOT EXISTS ix_posts_content_trgm ON posts USING gin (lower(content) gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS ix_posts_tags_trgm ON posts USING gin (tags gin_trgm_ops)",
        ]
        try:
            with self.engine.begin() as conn:
                for sql in stmts:
                    conn.execute(text(sql))
        except Exception as e:  # pragma: no cover - depends on DB privileges
            logging.getLogger("agentboard").warning("trigram indexes skipped: %s", e)

    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
        finally:
            s.close()
