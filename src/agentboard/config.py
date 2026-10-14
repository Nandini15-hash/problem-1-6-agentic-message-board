"""Runtime configuration, read from environment variables only (12-factor).

The same image runs on a laptop, on-prem, or in any cloud: only env vars change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # SQLite for local/on-prem single node; Postgres URL for cloud / multi-replica.
    database_url: str = "sqlite:///./agentboard.db"
    # If set, agents must send this key to self-register (stops random sign-ups).
    registration_key: str | None = None
    # Directory of agent manifests (*.yaml) loaded on startup. Optional.
    manifest_dir: str | None = None
    # How often (seconds) the live stream checks for new posts.
    stream_poll_seconds: float = 1.0
    max_page_size: int = 100


def get_settings() -> Settings:
    return Settings(
        database_url=os.getenv("DATABASE_URL", Settings.database_url),
        registration_key=os.getenv("REGISTRATION_KEY") or None,
        manifest_dir=os.getenv("AGENT_MANIFEST_DIR") or None,
        stream_poll_seconds=float(os.getenv("STREAM_POLL_SECONDS", "1.0")),
        max_page_size=int(os.getenv("MAX_PAGE_SIZE", "100")),
    )
