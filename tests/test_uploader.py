from pathlib import Path

import httpx
import pytest

from onedrive_provisioner.graph import GraphClient
from onedrive_provisioner.models import Status
from onedrive_provisioner.uploader import OneDriveUploader
from onedrive_provisioner.uploader.sources import LocalFolderSource


class _FakeTokenProvider:
    async def get_token(self) -> str:
        return "t"


@pytest.mark.asyncio
async def test_dry_run_no_calls(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    g = GraphClient(_FakeTokenProvider(), client=http)
    try:
        up = OneDriveUploader(g, dry_run=True)
        results = await up.upload_tree("USERID", LocalFolderSource(tmp_path), "Onboarding")
        assert len(results) == 1
        assert results[0].status == Status.DRY_RUN
        assert calls["n"] == 0
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_simple_upload_skips_existing(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    def handler(request: httpx.Request):
        if request.method == "GET" and "drive/root:/Onboarding/a.txt" in str(request.url) and "content" not in str(request.url):
            return httpx.Response(200, json={"size": 5})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    g = GraphClient(_FakeTokenProvider(), client=http)
    try:
        up = OneDriveUploader(g, large_file_threshold_mb=4)
        results = await up.upload_tree("USERID", LocalFolderSource(tmp_path), "Onboarding")
        assert results[0].status == Status.SKIPPED
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_simple_upload_uploads(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    seen = {"put": False}

    def handler(request: httpx.Request):
        url = str(request.url)
        if request.method == "GET" and "/drive/root:/Onboarding/a.txt" in url:
            return httpx.Response(404, json={"error": {"code": "itemNotFound", "message": "x"}})
        if request.method == "PUT" and url.endswith(":/content"):
            seen["put"] = True
            assert request.content == b"hello"
            return httpx.Response(201, json={"id": "abc", "size": 5})
        raise AssertionError(f"unexpected: {request.method} {url}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    g = GraphClient(_FakeTokenProvider(), client=http)
    try:
        up = OneDriveUploader(g)
        results = await up.upload_tree("USERID", LocalFolderSource(tmp_path), "Onboarding")
        assert results[0].status == Status.SUCCESS
        assert seen["put"] is True
    finally:
        await http.aclose()
