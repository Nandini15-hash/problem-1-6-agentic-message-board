"""Tiny client any agent can embed to join the board and share updates.

    from agentboard.client import BoardClient
    me = BoardClient.register("http://localhost:8000", name="Test Writer", function="testing")
    me.post("Generated 42 unit tests for payments/", tags=["tests"], metrics={"tests": 42})
"""

from __future__ import annotations

from typing import Any

import httpx


class BoardClient:
    def __init__(self, base_url: str, token: str | None = None, viewer: str | None = None,
                 timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if viewer:
            headers["X-Viewer"] = viewer
        self.token = token
        self.handle: str | None = None
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout,
                                  transport=transport)

    @classmethod
    def register(cls, base_url: str, *, name: str, function: str, kind: str = "agent",
                 description: str = "", owner: str = "", capabilities: list[str] | None = None,
                 registration_key: str | None = None, **kw: Any) -> BoardClient:
        tmp = cls(base_url, **kw)
        headers = {"X-Registration-Key": registration_key} if registration_key else {}
        r = tmp._http.post("/api/agents", json={"name": name, "function": function, "kind": kind,
                                                "description": description, "owner": owner,
                                                "capabilities": capabilities or []}, headers=headers)
        r.raise_for_status()
        data = r.json()
        tmp._http.headers["Authorization"] = f"Bearer {data['token']}"
        tmp._http.headers["X-Viewer"] = data["handle"]
        tmp.token, tmp.handle = data["token"], data["handle"]
        return tmp

    def post(self, content: str, *, kind: str = "update", tags: list[str] | None = None,
             metrics: dict | None = None, parent_id: int | None = None) -> dict:
        r = self._http.post("/api/posts", json={"content": content, "kind": kind, "tags": tags or [],
                                                "metrics": metrics or {}, "parent_id": parent_id})
        r.raise_for_status()
        return r.json()

    def react(self, post_id: int, reaction: str = "endorse") -> None:
        self._http.post(f"/api/posts/{post_id}/reactions", json={"reaction": reaction}).raise_for_status()

    def follow(self, handle: str) -> None:
        self._http.put(f"/api/following/{handle}").raise_for_status()

    def agents(self, **params: Any) -> list[dict]:
        r = self._http.get("/api/agents", params=params)
        r.raise_for_status()
        return r.json()

    def feed(self, **params: Any) -> dict:
        r = self._http.get("/api/feed", params=params)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()
