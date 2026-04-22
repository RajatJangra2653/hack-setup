"""File sources: local folder or Azure Blob container.

A FileSource enumerates `SourceFile` records and produces an async byte
iterator for each. This abstraction lets the uploader stream data without
loading entire files in memory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterator, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class SourceFile:
    """A logical file with a relative path inside the source root."""

    relative_path: str  # forward-slash separated, relative to source root
    size: int

    @property
    def name(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]


class FileSource(Protocol):
    def iter_files(self) -> Iterator[SourceFile]: ...
    async def open(self, sf: SourceFile, chunk_size: int) -> AsyncIterator[bytes]: ...
    @property
    def description(self) -> str: ...


# --------------------------------------------------------------------- local


class LocalFolderSource:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        if not self._root.exists() or not self._root.is_dir():
            raise FileNotFoundError(f"Local source folder not found: {self._root}")

    @property
    def description(self) -> str:
        return f"local:{self._root}"

    def iter_files(self) -> Iterator[SourceFile]:
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self._root).as_posix()
            yield SourceFile(relative_path=rel, size=path.stat().st_size)

    async def open(self, sf: SourceFile, chunk_size: int) -> AsyncIterator[bytes]:
        full = self._root / sf.relative_path
        # Use a thread to avoid blocking the loop on file IO
        import anyio

        async with await anyio.open_file(full, "rb") as fh:
            while True:
                chunk = await fh.read(chunk_size)
                if not chunk:
                    return
                yield chunk


# ---------------------------------------------------------------- azure blob


class AzureBlobSource:
    """Reads files from an Azure Blob container path.

    URL format: https://<account>.blob.core.windows.net/<container>[/<prefix>]
    Auth uses DefaultAzureCredential (env, managed identity, az login, etc.).
    """

    def __init__(self, url: str) -> None:
        from azure.identity import DefaultAzureCredential  # lazy
        from azure.storage.blob import ContainerClient

        parsed = urlparse(url)
        if not parsed.scheme.startswith("http") or "blob.core.windows.net" not in parsed.netloc:
            raise ValueError(f"Not a valid Azure Blob URL: {url}")
        parts = parsed.path.lstrip("/").split("/", 1)
        container = parts[0]
        self._prefix = (parts[1] if len(parts) > 1 else "").strip("/")
        account_url = f"{parsed.scheme}://{parsed.netloc}"
        self._cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._client = ContainerClient(
            account_url=account_url, container_name=container, credential=self._cred
        )
        self._url = url

    @property
    def description(self) -> str:
        return f"azure-blob:{self._url}"

    def iter_files(self) -> Iterator[SourceFile]:
        prefix = (self._prefix + "/") if self._prefix else ""
        for blob in self._client.list_blobs(name_starts_with=prefix):
            if blob.name.endswith("/"):
                continue
            rel = blob.name[len(prefix):] if prefix else blob.name
            if not rel:
                continue
            yield SourceFile(relative_path=rel, size=int(blob.size or 0))

    async def open(self, sf: SourceFile, chunk_size: int) -> AsyncIterator[bytes]:
        import anyio

        prefix = (self._prefix + "/") if self._prefix else ""
        blob_name = prefix + sf.relative_path
        # SDK is sync; do all blocking I/O on a worker thread.
        downloader = await anyio.to_thread.run_sync(
            lambda: self._client.download_blob(blob_name, max_concurrency=1)
        )
        chunk_iter = await anyio.to_thread.run_sync(downloader.chunks)
        sentinel = object()

        def _next(it):
            try:
                return next(it)
            except StopIteration:
                return sentinel

        while True:
            chunk = await anyio.to_thread.run_sync(_next, chunk_iter)
            if chunk is sentinel:
                return
            if chunk:
                yield bytes(chunk)


# ------------------------------------------------------------------- factory


def build_source(spec: str) -> FileSource:
    """Build a FileSource from a string spec.

    * 'https://*.blob.core.windows.net/...' -> AzureBlobSource
    * anything else -> LocalFolderSource (path)
    """
    if spec.startswith("http://") or spec.startswith("https://"):
        if "blob.core.windows.net" in spec:
            return AzureBlobSource(spec)
        raise ValueError(f"Unsupported remote source: {spec}")
    return LocalFolderSource(os.path.expanduser(spec))
