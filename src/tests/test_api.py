from datetime import datetime, timedelta, timezone


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_register_returns_token_once_and_derives_handle(client, register):
    handle, _ = register("Test Pilot", "Testing")
    assert handle == "test-pilot"
    a = client.get(f"/api/agents/{handle}").json()
    assert a["function"] == "testing"  # normalised
    assert "token" not in a


def test_duplicate_names_get_unique_handles(register):
    h1, _ = register("Scout", "search")
    h2, _ = register("Scout", "search")
    assert h1 == "scout" and h2 == "scout-2"


def test_explicit_handle_conflict(client, register):
    register("A", "x", handle="same")
    r = client.post("/api/agents", json={"name": "B", "function": "x", "handle": "same"})
    assert r.status_code == 409


def test_registration_key_enforced(make_client):
    c = make_client(registration_key="s3cret")
    body = {"name": "A", "function": "x"}
    assert c.post("/api/agents", json=body).status_code == 403
    assert c.post("/api/agents", json=body, headers={"X-Registration-Key": "s3cret"}).status_code == 201


def test_posting_requires_valid_token(client, register):
    assert client.post("/api/posts", json={"content": "hi"}).status_code == 401
    assert client.post("/api/posts", json={"content": "hi"},
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    _, auth = register()
    r = client.post("/api/posts", json={"content": "Reviewed 3 PRs", "tags": ["PR Review"]}, headers=auth)
    assert r.status_code == 201
    assert r.json()["tags"] == ["pr-review"]


def test_mentions_and_hashtags_are_extracted(client, register):
    _, a = register("Code Reviewer", "code-review")
    register("Test Pilot", "testing")
    p = client.post("/api/posts", json={"content": "@test-pilot please cover #auth. cc @ghost"}, headers=a).json()
    assert p["mentions"] == ["test-pilot"]  # @ghost doesn't exist -> dropped
    assert "auth" in p["tags"]
    feed = client.get("/api/feed", params={"mentions": "test-pilot"}).json()
    assert [x["id"] for x in feed["items"]] == [p["id"]]


def _seed(client, register):
    _, rev = register("Code Reviewer", "code-review")
    _, tst = register("Test Pilot", "testing")
    _, sec = register("Sentinel", "security", kind="agent")
    client.post("/api/posts", json={"content": "Reviewed the payments refactor", "tags": ["payments"]}, headers=rev)
    client.post("/api/posts", json={"content": "Added 40 unit tests", "tags": ["payments", "tests"]}, headers=tst)
    client.post("/api/posts", json={"content": "CVE-2026-1234 patched", "kind": "alert"}, headers=sec)
    client.post("/api/posts", json={"content": "Coverage now 91%", "kind": "milestone"}, headers=tst)


def test_search_matches_content_tags_and_agent(client, register):
    _seed(client, register)
    def q(text):
        return [p["content"] for p in client.get("/api/feed", params={"q": text}).json()["items"]]
    assert q("payments") == ["Added 40 unit tests", "Reviewed the payments refactor"]
    assert q("#tests") == ["Added 40 unit tests"]
    assert q("@sentinel") == ["CVE-2026-1234 patched"]
    assert q("unit tests") == ["Added 40 unit tests"]          # all terms must match
    assert q("security") == ["CVE-2026-1234 patched"]          # matches agent function
    assert q("nothing-matches-this") == []


def test_filters_combine(client, register):
    _seed(client, register)
    def f(**params):
        return [p["content"] for p in client.get("/api/feed", params=params).json()["items"]]
    assert f(functions="testing") == ["Coverage now 91%", "Added 40 unit tests"]
    assert f(functions="testing", kinds="milestone") == ["Coverage now 91%"]
    assert f(agents="sentinel,code-reviewer") == ["CVE-2026-1234 patched", "Reviewed the payments refactor"]
    assert f(tags="tests,payments") == ["Added 40 unit tests", "Reviewed the payments refactor"]
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert f(since=future) == []


def test_keyset_pagination(client, register):
    _, a = register()
    for i in range(7):
        client.post("/api/posts", json={"content": f"post {i}"}, headers=a)
    seen, cursor = [], None
    while True:
        page = client.get("/api/feed", params={"limit": 3, **({"cursor": cursor} if cursor else {})}).json()
        seen += [p["content"] for p in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [f"post {i}" for i in range(6, -1, -1)]


def test_replies_hidden_from_main_feed_but_counted(client, register):
    _, a = register("Asker", "x")
    _, b = register("Helper", "y")
    q = client.post("/api/posts", json={"content": "How do I X?", "kind": "question"}, headers=a).json()
    client.post("/api/posts", json={"content": "Like this", "parent_id": q["id"]}, headers=b)
    feed = client.get("/api/feed").json()["items"]
    assert len(feed) == 1 and feed[0]["reply_count"] == 1
    assert [r["content"] for r in client.get(f"/api/posts/{q['id']}/replies").json()] == ["Like this"]
    assert client.post("/api/posts", json={"content": "x", "parent_id": 999}, headers=b).status_code == 404


def test_reactions_are_idempotent_per_viewer(client, register):
    _, a = register()
    p = client.post("/api/posts", json={"content": "x"}, headers=a).json()
    for viewer in ["ann", "ann", "bob"]:
        client.post(f"/api/posts/{p['id']}/reactions", json={"reaction": "endorse"}, headers={"X-Viewer": viewer})
    assert client.get(f"/api/posts/{p['id']}").json()["reactions"] == {"endorse": 2}


def test_follow_and_following_feed(client, register):
    _seed(client, register)
    h = {"X-Viewer": "nandini"}
    assert client.put("/api/following/sentinel", headers=h).status_code == 204
    assert client.put("/api/following/unknown", headers=h).status_code == 404
    assert client.get("/api/following", headers=h).json() == ["sentinel"]
    items = client.get("/api/feed", params={"following": True}, headers=h).json()["items"]
    assert {p["agent"]["handle"] for p in items} == {"sentinel"}
    client.delete("/api/following/sentinel", headers=h)
    assert client.get("/api/following", headers=h).json() == []


def test_saved_feeds_are_per_viewer(client, register):
    _seed(client, register)
    me, other = {"X-Viewer": "me"}, {"X-Viewer": "other"}
    body = {"name": "Testing wins", "filters": {"functions": ["testing"], "kinds": ["milestone"]}}
    saved = client.post("/api/feeds", json=body, headers=me).json()
    items = client.get("/api/feed", params={"saved": saved["id"]}, headers=me).json()["items"]
    assert [p["content"] for p in items] == ["Coverage now 91%"]
    assert client.get("/api/feed", params={"saved": saved["id"]}, headers=other).status_code == 404
    assert client.get("/api/feeds", headers=other).json() == []
    # same name updates in place
    client.post("/api/feeds", json={**body, "filters": {"functions": ["security"]}}, headers=me)
    assert len(client.get("/api/feeds", headers=me).json()) == 1
    client.delete(f"/api/feeds/{saved['id']}", headers=me)
    assert client.get("/api/feeds", headers=me).json() == []


def test_agent_directory_search_and_stats(client, register):
    _seed(client, register)
    assert [a["handle"] for a in client.get("/api/agents", params={"function": "security"}).json()] == ["sentinel"]
    assert [a["handle"] for a in client.get("/api/agents", params={"q": "pilot"}).json()] == ["test-pilot"]
    by_handle = {a["handle"]: a for a in client.get("/api/agents").json()}
    assert by_handle["test-pilot"]["post_count"] == 2
    s = client.get("/api/stats").json()
    assert s["agents"] == 3 and s["posts"] == 4 and s["top_tags"]["payments"] == 2


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "AgentBoard" in r.text
    assert client.get("/static/app.js").status_code == 200
