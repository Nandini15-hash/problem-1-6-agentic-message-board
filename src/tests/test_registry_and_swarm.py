import random
from pathlib import Path

import pytest

from agentboard import service as svc
from agentboard.registry import load_manifest_dir, parse_manifest
from agentboard.schemas import FeedFilters
from agentboard.swarm import OfflineGenerator, Swarm, expand_catalog, load_catalog
from agentboard.swarm.llm import extract_json
from agentboard.swarm.simulator import ClaudeGenerator, Draft

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog" / "agents.yaml"


def test_no_agent_names_hard_coded_in_source():
    """The requirement: code must not hard-code agent names. Enforce it in CI."""
    names = [a["name"] for a in load_catalog(CATALOG)]
    for py in (ROOT / "agentboard").rglob("*.py"):
        text = py.read_text()
        for name in names:
            assert name not in text, f"{name!r} is hard-coded in {py}"


def test_parse_manifest_single_and_many_and_ignores_extra_keys():
    one = parse_manifest("name: Solo\nfunction: x\ntasks: [a]\n")
    many = parse_manifest('{"agents": [{"name": "A", "function": "x"}, {"name": "B", "function": "y"}]}', ".json")
    assert [a.name for a in one] == ["Solo"]
    assert [a.name for a in many] == ["A", "B"]


def test_manifest_dir_reports_bad_files(tmp_path):
    (tmp_path / "good.yaml").write_text("name: Good\nfunction: x\n")
    (tmp_path / "bad.yaml").write_text("name: NoFunction\n")
    (tmp_path / "ignored.txt").write_text("whatever")
    agents, errors = load_manifest_dir(tmp_path)
    assert [a.name for a in agents] == ["Good"]
    assert len(errors) == 1 and "bad.yaml" in errors[0]


def test_manifests_load_on_startup_idempotently(make_client, tmp_path):
    d = tmp_path / "manifests"
    d.mkdir()
    (d / "team.yaml").write_text(CATALOG.read_text())
    c = make_client(manifest_dir=str(d))
    n = len(load_catalog(CATALOG))
    assert len(c.get("/api/agents").json()) == n
    # second app on the same DB -> still n agents (upsert, not duplicate)
    app_db = c.app.state.db
    with app_db.SessionLocal() as s:
        for a in parse_manifest(CATALOG.read_text()):
            svc.upsert_agent_from_manifest(s, a)
    assert len(c.get("/api/agents").json()) == n


def test_expand_catalog_scales_beyond_catalog():
    specs = load_catalog(CATALOG)
    big = expand_catalog(specs, 50)
    assert len(big) == 50 and len({s["name"] for s in big}) == 50


def test_offline_generator_is_deterministic():
    spec = load_catalog(CATALOG)[0]
    a = OfflineGenerator().write(spec, "update", None, random.Random(1))
    b = OfflineGenerator().write(spec, "update", None, random.Random(1))
    assert a == b and a.content


def test_swarm_runs_end_to_end_against_api(client):
    specs = load_catalog(CATALOG)
    swarm = Swarm("http://testserver", specs, OfflineGenerator(), seed=7, transport=client._transport, workers=1)
    stats = swarm.run(rounds=4)
    assert sum(stats.values()) == 4 * len(specs)
    feed = client.get("/api/feed", params={"limit": 100, "include_replies": True}).json()["items"]
    assert len(feed) >= len(specs)  # endorsements don't create posts
    assert len(client.get("/api/agents").json()) == len(specs)
    handoffs = [p for p in feed if p["kind"] == "handoff"]
    assert handoffs and all(p["mentions"] for p in handoffs)  # peers found dynamically


def test_handoff_peer_is_chosen_by_function(client):
    specs = [{"name": "Alpha", "function": "a", "handoff_to": ["b"]},
             {"name": "Beta", "function": "b"}, {"name": "Gamma", "function": "c"}]
    swarm = Swarm("http://testserver", specs, transport=client._transport)
    swarm.register_all()
    directory = swarm.members[0][1].agents()
    me = swarm.members[0][1]
    for seed in range(10):
        peer = swarm._peer_for(specs[0], me, directory, random.Random(seed))
        assert peer["function"] == "b"


@pytest.mark.parametrize("text,expected", [
    ('```json\n{"content": "hi"}\n```', {"content": "hi"}),
    ('Sure! {"content": "x", "tags": []} done', {"content": "x", "tags": []}),
    ("no json here", None),
    (None, None),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_claude_generator_falls_back_when_cli_fails(monkeypatch):
    import agentboard.swarm.simulator as sim
    monkeypatch.setattr(sim, "run_claude", lambda *a, **k: None)
    gen = ClaudeGenerator()
    d = gen.write(load_catalog(CATALOG)[0], "update", None, random.Random(0))
    assert isinstance(d, Draft) and d.content and gen.failures == 1


def test_claude_generator_forces_mention_on_handoff(monkeypatch):
    import agentboard.swarm.simulator as sim
    monkeypatch.setattr(sim, "run_claude", lambda *a, **k: '{"content": "passing this on", "metrics": {"n": 3, "bad": "x"}}')
    d = ClaudeGenerator().write({"name": "A", "function": "a"}, "handoff", {"handle": "beta", "function": "b"},
                                random.Random(0))
    assert d.content.startswith("@beta") and d.metrics == {"n": 3}


def test_stream_poll_helper_returns_only_new_posts(client, register):
    _, a = register()
    first = client.post("/api/posts", json={"content": "one"}, headers=a).json()
    client.post("/api/posts", json={"content": "two"}, headers=a)
    with client.app.state.db.SessionLocal() as s:
        new = svc.query_feed(s, FeedFilters(), after_id=first["id"])
    assert [p.content for p in new] == ["two"]
