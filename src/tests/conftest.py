import os

import pytest
from fastapi.testclient import TestClient

from agentboard.api import create_app
from agentboard.config import Settings


@pytest.fixture()
def make_client(tmp_path):
    clients = []

    def _make(**overrides):
        # Default: throwaway SQLite file. Set TEST_DATABASE_URL to run the same suite on Postgres.
        url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test-{len(clients)}.db"
        settings = Settings(database_url=url, stream_poll_seconds=0.05, **overrides)
        app = create_app(settings)
        if os.getenv("TEST_DATABASE_URL"):
            from agentboard.db import Base
            Base.metadata.drop_all(app.state.db.engine)
        c = TestClient(app)
        c.__enter__()  # run lifespan (create tables, load manifests)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture()
def client(make_client):
    return make_client()


@pytest.fixture()
def register(client):
    def _register(name="Code Reviewer", function="code-review", **kw):
        r = client.post("/api/agents", json={"name": name, "function": function, **kw})
        assert r.status_code == 201, r.text
        data = r.json()
        return data["handle"], {"Authorization": f"Bearer {data['token']}"}
    return _register
