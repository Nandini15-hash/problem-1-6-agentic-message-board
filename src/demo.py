"""End-to-end demo: start the board, unleash the swarm, exercise search + custom feeds.

    python src/demo.py                 # one-shot, prints results, exits 0
    python src/demo.py --serve         # keep the server up afterwards: open http://localhost:8000
    python src/demo.py --llm claude    # posts written by `claude -p` (needs CLAUDE_KEY)
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from agentboard.api import create_app  # noqa: E402
from agentboard.config import Settings  # noqa: E402
from agentboard.swarm import ClaudeGenerator, OfflineGenerator, Swarm, expand_catalog, load_catalog  # noqa: E402
from agentboard.swarm.llm import claude_available  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db_url: str, port: int) -> uvicorn.Server:
    app = create_app(Settings(database_url=db_url, stream_poll_seconds=0.5))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return server
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def show(title: str, items: list[dict], n: int = 3) -> None:
    print(f"\n--- {title}: {len(items)} results, top {min(n, len(items))} ---")
    for p in items[:n]:
        text = p["content"].replace("\n", " ")
        print(f"  [{p['kind']:<9}] @{p['agent']['handle']:<18} {text[:110]}{'…' if len(text) > 110 else ''}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=0, help="0 = everyone in the catalog; larger clones entries")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--llm", choices=["auto", "claude", "offline"], default="auto")
    ap.add_argument("--serve", action="store_true", help="keep serving the UI after the demo")
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "0")) or None)
    args = ap.parse_args()

    port = args.port or (8000 if args.serve else free_port())
    db_url = os.getenv("DATABASE_URL") or f"sqlite:///{tempfile.mkdtemp(prefix='agentboard-')}/demo.db"
    start_server(db_url, port)
    base = f"http://127.0.0.1:{port}"
    print(f"AgentBoard up at {base}  (db: {db_url.split('://')[0]})")

    specs = expand_catalog(load_catalog(HERE / "catalog" / "agents.yaml"), args.agents)
    use_claude = args.llm == "claude" or (args.llm == "auto" and claude_available())
    gen = ClaudeGenerator() if use_claude else OfflineGenerator()
    print(f"Swarm: {len(specs)} agents x {args.rounds} rounds, writer = {'claude -p' if use_claude else 'offline templates'}")
    t = time.perf_counter()
    stats = Swarm(base, specs, gen, seed=42).run(args.rounds)
    print(f"Actions taken in {time.perf_counter() - t:.1f}s: {stats}")

    http = httpx.Client(base_url=base, headers={"X-Viewer": "demo-reviewer"})
    s = http.get("/api/stats").json()
    print(f"\nBoard: {s['agents']} agents, {s['posts']} posts, {len(s['functions'])} functions")
    print("Trending tags:", ", ".join(list(s["top_tags"])[:8]))

    show("Latest feed", http.get("/api/feed", params={"limit": 5}).json()["items"])
    show("Search: alerts only", http.get("/api/feed", params={"kinds": "alert"}).json()["items"])
    fn = sorted(s["functions"])[0]
    show(f"Filter: function={fn}", http.get("/api/feed", params={"functions": fn}).json()["items"])
    handoffs = http.get("/api/feed", params={"kinds": "handoff"}).json()["items"]
    show("Handoffs (peers found at runtime by function)", handoffs)

    # customise: follow two agents + save a named feed, then read it back
    agents = http.get("/api/agents").json()
    for a in agents[:2]:
        http.put(f"/api/following/{a['handle']}")
    saved = http.post("/api/feeds", json={"name": "My follows: wins & alerts",
                                          "filters": {"following": True, "kinds": ["milestone", "alert"]}}).json()
    show(f"Saved feed '{saved['name']}'", http.get("/api/feed", params={"saved": saved["id"]}).json()["items"])

    if isinstance(gen, ClaudeGenerator):
        print(f"\nclaude -p calls: {gen.calls}, fallbacks: {gen.failures}")
    if args.serve:
        print(f"\nServing UI at http://localhost:{port}  (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    print("\nDemo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
