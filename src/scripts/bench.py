"""Scale check: seed many agents/posts straight into the DB, then time feed queries.

    python scripts/bench.py --agents 1000 --posts 200000
    DATABASE_URL=postgresql+psycopg2://... python scripts/bench.py
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import insert  # noqa: E402

from agentboard import service as svc  # noqa: E402
from agentboard.db import Database  # noqa: E402
from agentboard.models import Agent, Post  # noqa: E402
from agentboard.schemas import FeedFilters  # noqa: E402

FUNCTIONS = [f"function-{i}" for i in range(40)]
WORDS = ("deploy review test index scan patch triage release report invoice chart canary rollback "
         "latency coverage contract dashboard migration schema cache queue token").split()


def seed(db: Database, n_agents: int, n_posts: int, rng: random.Random) -> None:
    with db.engine.begin() as conn:
        conn.execute(insert(Agent), [
            {"handle": f"agent-{i}", "name": f"Agent {i}", "kind": "agent", "function": rng.choice(FUNCTIONS),
             "description": "", "owner": "bench", "capabilities": [], "token_hash": f"{i:064d}"}
            for i in range(n_agents)])
        batch = []
        for _i in range(n_posts):
            words = rng.sample(WORDS, 8)
            batch.append({"agent_id": rng.randint(1, n_agents), "kind": rng.choice(["update"] * 6 + ["alert", "milestone"]),
                          "content": " ".join(words), "tags": svc._pack(words[:2]), "mentions": ",", "metrics": {}})
            if len(batch) == 10_000:
                conn.execute(insert(Post), batch)
                batch = []
        if batch:
            conn.execute(insert(Post), batch)


def timeit(fn, runs: int = 30) -> tuple[float, float]:
    samples = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    samples.sort()
    return statistics.median(samples), samples[int(len(samples) * 0.95) - 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--posts", type=int, default=200_000)
    args = ap.parse_args()
    url = os.getenv("DATABASE_URL", "sqlite:///./bench.db")
    if url.startswith("sqlite:///./"):
        Path(url.removeprefix("sqlite:///")).unlink(missing_ok=True)
    db = Database(url)
    from agentboard.db import Base
    Base.metadata.drop_all(db.engine)
    db.create_all()
    rng = random.Random(0)
    t = time.perf_counter()
    seed(db, args.agents, args.posts, rng)
    if db.engine.dialect.name == "postgresql":
        with db.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("ANALYZE")  # fresh planner stats after bulk load
    print(f"seeded {args.agents} agents / {args.posts} posts in {time.perf_counter() - t:.1f}s  ({url.split(':')[0]})")

    cases = {
        "latest page": FeedFilters(),
        "one function": FeedFilters(functions=["function-7"]),
        "one agent": FeedFilters(agents=["agent-42"]),
        "kind=alert": FeedFilters(kinds=["alert"]),
        "tag": FeedFilters(tags=["canary"]),
        "text search 2 terms": FeedFilters(q="canary rollback"),
    }
    print(f"{'query':<22}{'p50 ms':>10}{'p95 ms':>10}")
    with db.SessionLocal() as s:
        for name, f in cases.items():
            p50, p95 = timeit(lambda f=f: svc.posts_out(s, svc.query_feed(s, f, limit=30)))
            print(f"{name:<22}{p50:>10.1f}{p95:>10.1f}")
        page = svc.query_feed(s, FeedFilters(), limit=30)
        deep_cursor = page[-1].id - args.posts // 2
        p50, p95 = timeit(lambda: svc.query_feed(s, FeedFilters(), limit=30, cursor=deep_cursor))
        print(f"{'deep page (keyset)':<22}{p50:>10.1f}{p95:>10.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
