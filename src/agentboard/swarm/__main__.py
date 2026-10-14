"""python -m agentboard.swarm --catalog catalog/agents.yaml --rounds 5"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .llm import claude_available
from .simulator import ClaudeGenerator, OfflineGenerator, Swarm, expand_catalog, load_catalog

DEFAULT_CATALOG = Path(__file__).resolve().parents[2] / "catalog" / "agents.yaml"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run a simulated swarm of agents against an AgentBoard server.")
    p.add_argument("--base-url", default=os.getenv("AGENTBOARD_URL", "http://localhost:8000"))
    p.add_argument("--catalog", default=os.getenv("SWARM_CATALOG", str(DEFAULT_CATALOG)))
    p.add_argument("--agents", type=int, default=0, help="Total agents (clones catalog entries if larger)")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--interval", type=float, default=1.0, help="Seconds between rounds")
    p.add_argument("--llm", choices=["auto", "claude", "offline"], default="auto",
                   help="auto = use `claude -p` if CLAUDE_KEY is set, else offline templates")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--registration-key", default=os.getenv("REGISTRATION_KEY"))
    args = p.parse_args(argv)

    specs = expand_catalog(load_catalog(args.catalog), args.agents)
    use_claude = args.llm == "claude" or (args.llm == "auto" and claude_available())
    gen = ClaudeGenerator() if use_claude else OfflineGenerator()
    print(f"swarm: {len(specs)} agents, {args.rounds} rounds, generator={'claude -p' if use_claude else 'offline'}")

    swarm = Swarm(args.base_url, specs, gen, seed=args.seed, registration_key=args.registration_key)
    swarm.register_all()
    print("registered:", ", ".join(c.handle or "?" for _, c in swarm.members[:12]),
          "..." if len(swarm.members) > 12 else "")

    def tick(r: int) -> None:
        print(f"round {r}/{args.rounds}: {swarm.stats}")
        if r < args.rounds:
            time.sleep(args.interval)

    swarm.run(args.rounds, on_round=tick)
    if isinstance(gen, ClaudeGenerator):
        print(f"claude calls={gen.calls} fallbacks={gen.failures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
