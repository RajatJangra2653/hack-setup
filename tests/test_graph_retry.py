import asyncio

import httpx
import pytest

from onedrive_provisioner.graph import GraphClient, GraphError


class _FakeTokenProvider:
    async def get_token(self) -> str:
        return "fake-token"


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return GraphClient(_FakeTokenProvider(), max_retries=3, client=http), http


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(monkeypatch):
    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr("onedrive_provisioner.graph.client.asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    g, http = _make_client(handler)
    try:
        out = await g.get("/users/x")
        assert out == {"ok": True}
        assert calls["n"] == 3
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_raises_graph_error_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "ResourceNotFound", "message": "no"}})

    g, http = _make_client(handler)
    try:
        with pytest.raises(GraphError) as exc:
            await g.get("/users/x")
        assert exc.value.status == 404
        assert exc.value.code == "ResourceNotFound"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_pagination(monkeypatch):
    pages = [
        {"value": [{"id": "1"}, {"id": "2"}], "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?p=2"},
        {"value": [{"id": "3"}]},
    ]
    idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json=page)

    g, http = _make_client(handler)
    try:
        ids = [u["id"] async for u in g.paged("/users")]
        assert ids == ["1", "2", "3"]
    finally:
        await http.aclose()
