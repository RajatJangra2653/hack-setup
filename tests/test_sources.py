from pathlib import Path

import pytest

from onedrive_provisioner.uploader.sources import LocalFolderSource, build_source


@pytest.mark.asyncio
async def test_local_folder_iteration_and_open(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"\x00\x01\x02\x03")

    src = LocalFolderSource(tmp_path)
    files = list(src.iter_files())
    rels = sorted(f.relative_path for f in files)
    assert rels == ["a.txt", "sub/b.bin"]
    sizes = {f.relative_path: f.size for f in files}
    assert sizes["a.txt"] == 5
    assert sizes["sub/b.bin"] == 4

    target = next(f for f in files if f.relative_path == "sub/b.bin")
    chunks = []
    async for c in src.open(target, 1024):
        chunks.append(c)
    assert b"".join(chunks) == b"\x00\x01\x02\x03"


def test_build_source_local(tmp_path):
    s = build_source(str(tmp_path))
    assert isinstance(s, LocalFolderSource)


def test_build_source_unknown_remote():
    with pytest.raises(ValueError):
        build_source("https://example.com/foo")
