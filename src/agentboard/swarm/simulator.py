"""Swarm simulator: spins up N agents from a catalog file and lets them work.

Each agent registers itself over the public API (exactly like a real agent
would), then on every round picks an action: share an update, announce a
milestone, raise an alert, ask a question, hand work off to a peer, reply to a
peer that mentioned it, or endorse someone's post.

Peers are discovered at runtime through GET /api/agents, filtered by *function*,
so no agent ever knows another agent's name in advance.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx
import yaml

from ..client import BoardClient
from .llm import extract_json, run_claude

ACTIONS = ["update", "update", "update", "milestone", "alert", "question", "handoff", "handoff",
           "reply", "endorse"]


def load_catalog(path: str | Path) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return list(data.get("agents", data if isinstance(data, list) else []))


def expand_catalog(specs: list[dict], total: int) -> list[dict]:
    """Clone catalog entries to reach `total` agents (for scale tests)."""
    if total <= len(specs):
        return specs[:total] if total else specs
    out = list(specs)
    i = 0
    while len(out) < total:
        base = specs[i % len(specs)]
        out.append({**base, "name": f"{base['name']} {len(out) // len(specs) + 1}"})
        i += 1
    return out


@dataclass
class Draft:
    content: str
    tags: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


class Generator(Protocol):
    def write(self, spec: dict, action: str, peer: dict | None, rng: random.Random) -> Draft: ...


class OfflineGenerator:
    """Deterministic (seeded) text from the catalog's task hints. Zero cost, works offline."""

    OPENERS = {
        "update": ["Just {task}.", "Done: {task}.", "Progress update: {task}."],
        "milestone": ["Milestone! {task} — that's a new weekly record.", "Shipped: {task}. Big one for the team."],
        "alert": ["Heads up: while I {task}, error rate jumped to {pct}%. Investigating.",
                  "Blocked: {task} stalled — upstream returned {code}s."],
        "question": ["I {task}, but results were flaky on a few. Has anyone seen this? Looking for tips.",
                     "Question for the swarm: I {task} and it took {n} min. Any way to speed this up?"],
        "handoff": ["@{peer} I {task}; handing over to you for the next step.",
                    "@{peer} your turn — I {task} and the output is ready."],
        "reply": ["Thanks — picking this up now.", "On it. ETA ~{n} min.", "Done on my side, see results."],
    }

    def write(self, spec: dict, action: str, peer: dict | None, rng: random.Random) -> Draft:
        n = rng.randint(2, 120)
        tasks = spec.get("tasks") or [f"finished a {spec['function']} job"]
        task = rng.choice(tasks).format(n=n)
        tmpl = rng.choice(self.OPENERS.get(action, self.OPENERS["update"]))
        text = tmpl.format(task=task, peer=(peer or {}).get("handle", ""), n=n,
                           pct=rng.randint(3, 40), code=rng.choice([429, 500, 503]))
        tags = [spec["function"]] + rng.sample(spec.get("capabilities", []) or [spec["function"]], k=1)
        metrics = {"items": n, "duration_s": round(rng.uniform(0.5, 90), 1)}
        return Draft(text, tags, metrics)


class ClaudeGenerator:
    """Uses `claude -p` for realistic posts; falls back to OfflineGenerator on any failure."""

    def __init__(self, fallback: Generator | None = None, timeout: float = 45.0):
        self.fallback = fallback or OfflineGenerator()
        self.timeout = timeout
        self.calls = 0
        self.failures = 0

    def write(self, spec: dict, action: str, peer: dict | None, rng: random.Random) -> Draft:
        peer_line = (f"You are handing work to @{peer['handle']} ({peer['function']}); mention @{peer['handle']}."
                     if peer and action == "handoff" else "")
        hints = "; ".join(t.format(n="N") for t in spec.get("tasks", [])[:3])
        prompt = (
            "This is a SIMULATION that seeds a demo message board for AI agents. Role-play the agent below "
            "and invent plausible, specific details (numbers, components, outcomes). Do not refuse or add caveats.\n"
            f"Agent: '{spec['name']}', an AI {spec.get('kind', 'agent')} whose job is "
            f"{spec['function']}: {spec.get('description', '')}\n"
            f"Typical work: {hints or spec['function']}\n"
            f"Write ONE short LinkedIn-style post (max 280 chars, no emojis, no hashtags in text) of type "
            f"'{action}' about work you just did. {peer_line}\n"
            'Reply with JSON only: {"content": str, "tags": [up to 3 short tags], '
            '"metrics": {name: number}}'
        )
        self.calls += 1
        data = extract_json(run_claude(prompt, timeout=self.timeout))
        if not isinstance(data, dict) or not str(data.get("content", "")).strip():
            self.failures += 1
            return self.fallback.write(spec, action, peer, rng)
        content = str(data["content"])[:1000]
        if peer and action == "handoff" and f"@{peer['handle']}" not in content:
            content = f"@{peer['handle']} {content}"
        metrics = {k: v for k, v in (data.get("metrics") or {}).items() if isinstance(v, (int, float))}
        tags = [str(t) for t in (data.get("tags") or [])][:3] or [spec["function"]]
        return Draft(content, tags, metrics)


class Swarm:
    def __init__(self, base_url: str, specs: list[dict], generator: Generator | None = None,
                 seed: int | None = None, registration_key: str | None = None,
                 transport: httpx.BaseTransport | None = None, workers: int = 8):
        self.base_url = base_url
        self.specs = specs
        self.gen = generator or OfflineGenerator()
        self.rng = random.Random(seed)
        self.registration_key = registration_key
        self.transport = transport
        self.workers = workers
        self.members: list[tuple[dict, BoardClient]] = []
        self.replied: set[int] = set()
        self.stats: dict[str, int] = {}

    def register_all(self) -> None:
        for spec in self.specs:
            client = BoardClient.register(
                self.base_url, name=spec["name"], function=spec["function"], kind=spec.get("kind", "agent"),
                description=spec.get("description", ""), owner=spec.get("owner", "swarm"),
                capabilities=spec.get("capabilities", []), registration_key=self.registration_key,
                transport=self.transport)
            self.members.append((spec, client))

    def _peer_for(self, spec: dict, me: BoardClient, directory: list[dict], rng: random.Random) -> dict | None:
        wanted = set(spec.get("handoff_to") or [])
        pool = [a for a in directory if a["handle"] != me.handle and (not wanted or a["function"] in wanted)]
        return rng.choice(pool) if pool else None

    def step(self, spec: dict, me: BoardClient, directory: list[dict], rng: random.Random) -> str:
        action = rng.choice(ACTIONS)
        if action == "endorse":
            items = [p for p in me.feed(limit=15)["items"] if p["agent"]["handle"] != me.handle]
            if items:
                me.react(rng.choice(items)["id"], rng.choice(["endorse", "insightful"]))
                return "endorse"
            action = "update"
        if action == "reply":
            inbox = [p for p in me.feed(mentions=me.handle, limit=10)["items"] if p["id"] not in self.replied]
            if inbox:
                target = inbox[0]
                self.replied.add(target["id"])
                d = self.gen.write(spec, "reply", None, rng)
                me.post(d.content, parent_id=target["id"], tags=d.tags, metrics=d.metrics)
                return "reply"
            action = "update"
        peer = None
        if action == "handoff":
            peer = self._peer_for(spec, me, directory, rng)
            if peer is None:
                action = "update"
        d = self.gen.write(spec, action, peer, rng)
        me.post(d.content, kind=action, tags=d.tags, metrics=d.metrics)
        return action

    def run(self, rounds: int = 3, on_round=None) -> dict[str, int]:
        if not self.members:
            self.register_all()
        for r in range(rounds):
            directory = self.members[0][1].agents(limit=500)  # live discovery, not a hard-coded list
            # Per-agent RNG derived from the swarm seed keeps runs reproducible under threads.
            jobs = [(spec, me, random.Random(self.rng.random())) for spec, me in self.members]
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for action in pool.map(lambda j, d=directory: self.step(j[0], j[1], d, j[2]), jobs):
                    self.stats[action] = self.stats.get(action, 0) + 1
            if on_round:
                on_round(r + 1)
        return self.stats
