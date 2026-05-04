"""Upload files to a user's OneDrive via Microsoft Graph.

Strategy:
  * Files <= LARGE_FILE_THRESHOLD: simple PUT to /content
  * Larger files: createUploadSession + chunked PUT with byte ranges
  * Idempotency: HEAD/GET item metadata; if size matches, skip.
  * Folder structure recreated by uploading to an explicit destination path
    (Graph creates intermediate folders automatically when uploading by path).
"""
from __future__ import annotations

import asyncio
from typing import List
from urllib.parse import quote

import httpx

from ..graph import GraphClient, GraphError
from ..logging_setup import get_logger
from ..models import FileResult, Status
from .sources import FileSource, SourceFile

logger = get_logger(__name__)

_MB = 1024 * 1024
# Graph requires upload-session chunks to be a multiple of 320 KiB.
_CHUNK_ALIGN = 320 * 1024


_MIN_CHUNK = _CHUNK_ALIGN * 10  # 3.125 MiB practical minimum

def _align_chunk(size_mb: int) -> int:
    raw = max(1, size_mb) * _MB
    return max(_MIN_CHUNK, (raw // _CHUNK_ALIGN) * _CHUNK_ALIGN)


def _encode_path(path: str) -> str:
    """Percent-encode path segments for Graph item-by-path addressing."""
    return "/".join(quote(seg, safe="") for seg in path.split("/") if seg)


class OneDriveUploader:
    def __init__(
        self,
        graph: GraphClient,
        *,
        chunk_size_mb: int = 10,
        large_file_threshold_mb: int = 4,
        dry_run: bool = False,
    ) -> None:
        self._graph = graph
        self._chunk_size = _align_chunk(chunk_size_mb)
        self._threshold = large_file_threshold_mb * _MB
        self._dry_run = dry_run

    # ------------------------------------------------------------------ API
    async def upload_tree(
        self,
        user_id: str,
        source: FileSource,
        destination: str,
    ) -> List[FileResult]:
        """Upload all files from `source` under `destination` in user's drive."""
        dest = (destination or "").strip("/").replace("\\", "/")
        results: List[FileResult] = []
        for sf in source.iter_files():
            target_path = f"{dest}/{sf.relative_path}" if dest else sf.relative_path
            try:
                res = await self._upload_one(user_id, source, sf, target_path)
            except GraphError as exc:
                logger.error(
                    "upload.failed",
                    user_id=user_id,
                    path=target_path,
                    status=exc.status,
                    code=exc.code,
                    error=exc.message,
                )
                res = FileResult(
                    path=target_path, size=sf.size, status=Status.FAILED, message=str(exc)
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("upload.unexpected", user_id=user_id, path=target_path)
                res = FileResult(
                    path=target_path, size=sf.size, status=Status.FAILED, message=str(exc)
                )
            results.append(res)
        return results

    # -------------------------------------------------------------- internal
    async def _upload_one(
        self,
        user_id: str,
        source: FileSource,
        sf: SourceFile,
        target_path: str,
    ) -> FileResult:
        if self._dry_run:
            logger.info("upload.dry_run", user_id=user_id, path=target_path, size=sf.size)
            return FileResult(path=target_path, size=sf.size, status=Status.DRY_RUN)

        if await self._already_uploaded(user_id, target_path, sf.size):
            logger.info("upload.skip_existing", user_id=user_id, path=target_path)
            return FileResult(path=target_path, size=sf.size, status=Status.SKIPPED, message="exists")

        if sf.size <= self._threshold:
            await self._simple_upload(user_id, source, sf, target_path)
        else:
            await self._chunked_upload(user_id, source, sf, target_path)

        logger.info("upload.ok", user_id=user_id, path=target_path, size=sf.size)
        return FileResult(path=target_path, size=sf.size, status=Status.SUCCESS)

    async def _already_uploaded(self, user_id: str, path: str, size: int) -> bool:
        encoded = _encode_path(path)
        try:
            item = await self._graph.get(f"/users/{user_id}/drive/root:/{encoded}")
        except GraphError as exc:
            if exc.status == 404:
                return False
            raise
        return int(item.get("size", -1)) == size

    async def _simple_upload(
        self, user_id: str, source: FileSource, sf: SourceFile, target_path: str
    ) -> None:
        encoded = _encode_path(target_path)
        # Read whole file (it's <= threshold, default 4 MB)
        buf = bytearray()
        async for chunk in source.open(sf, self._chunk_size):
            buf.extend(chunk)
        await self._graph.put(
            f"/users/{user_id}/drive/root:/{encoded}:/content",
            content=bytes(buf),
            headers={"Content-Type": "application/octet-stream"},
            expect_json=True,
        )

    async def _chunked_upload(
        self, user_id: str, source: FileSource, sf: SourceFile, target_path: str
    ) -> None:
        encoded = _encode_path(target_path)
        session = await self._graph.post(
            f"/users/{user_id}/drive/root:/{encoded}:/createUploadSession",
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "replace",
                    "name": sf.name,
                }
            },
        )
        upload_url = session["uploadUrl"]

        offset = 0
        total = sf.size
        # Re-buffer source bytes into aligned chunks
        async for chunk in self._aligned_chunks(source, sf):
            end = offset + len(chunk) - 1
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end}/{total}",
                "Content-Type": "application/octet-stream",
            }
            await self._put_chunk(upload_url, chunk, headers)
            offset = end + 1

        if offset != total:
            # Mismatch — abort the session for cleanliness
            try:
                await self._graph.delete(upload_url)
            except Exception:
                pass
            raise GraphError(
                0, "uploadIncomplete", f"Uploaded {offset} bytes, expected {total}"
            )

    async def _aligned_chunks(self, source: FileSource, sf: SourceFile):
        buf = bytearray()
        target = self._chunk_size
        async for piece in source.open(sf, target):
            buf.extend(piece)
            while len(buf) >= target:
                yield bytes(buf[:target])
                del buf[:target]
        if buf:
            yield bytes(buf)

    async def _put_chunk(self, upload_url: str, chunk: bytes, headers: dict) -> None:
        # Upload session URLs are pre-authorized — no Bearer token required, and
        # using one can cause failures. Use a bare httpx call with retries.
        attempt = 0
        max_retries = 6
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            while True:
                attempt += 1
                try:
                    resp = await client.put(upload_url, content=chunk, headers=headers)
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    if attempt > max_retries:
                        raise GraphError(0, "network", str(exc)) from exc
                    await asyncio.sleep(min(60, 2 ** attempt))
                    continue
                if resp.status_code in (200, 201, 202):
                    return
                if resp.status_code in (408, 429, 500, 502, 503, 504) and attempt <= max_retries:
                    ra = resp.headers.get("Retry-After")
                    delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(60, 2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                # Hard error — surface
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                raise GraphError(resp.status_code, "uploadChunkFailed", str(body), body=body)
