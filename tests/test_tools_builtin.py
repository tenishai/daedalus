"""The files builtin: rooted, read-only, fail-closed. Dogfood finding #10 —
mcp-server-git cannot read file contents, so bundles need a no-dependency way."""

import os

import pytest

from daedalus.tools.builtin import FilesToolProvider
from daedalus.tools.compose import MultiToolProvider
from tests.fakes import FakeTools


@pytest.fixture
def root(tmp_path):
    (tmp_path / "Makefile").write_text(".PHONY: test\n\ntest:\n\tpytest -q\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("inner")
    return tmp_path


async def test_read_and_list_inside_root(root):
    files = FilesToolProvider([str(root)])
    assert ".PHONY: test" in await files.call("files.read", {"path": str(root / "Makefile")})
    listing = await files.call("files.list", {"path": str(root)})
    assert "Makefile" in listing and "sub/" in listing


async def test_traversal_and_outside_paths_fail_before_io(root):
    files = FilesToolProvider([str(root / "sub")])
    with pytest.raises(PermissionError, match="outside the allowed roots"):
        await files.call("files.read", {"path": str(root / "sub" / ".." / "Makefile")})
    with pytest.raises(PermissionError):
        await files.call("files.read", {"path": "/etc/passwd"})


async def test_symlink_escape_is_caught(root):
    outside = root.parent / "secret.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "link.txt")
    files = FilesToolProvider([str(root)])
    with pytest.raises(PermissionError):
        await files.call("files.read", {"path": str(root / "link.txt")})


async def test_size_cap(root):
    (root / "big.bin").write_text("x" * 1000)
    files = FilesToolProvider([str(root)], max_bytes=100)
    with pytest.raises(ValueError, match="read limit"):
        await files.call("files.read", {"path": str(root / "big.bin")})


async def test_empty_roots_rejected():
    with pytest.raises(ValueError, match="at least one root"):
        FilesToolProvider([])


async def test_multi_provider_routes_by_listing(root):
    files = FilesToolProvider([str(root)])
    fake = FakeTools([{"name": "git.git_status", "description": "", "parameters": {}}])
    multi = MultiToolProvider([files, fake])
    names = [spec["name"] for spec in await multi.tools()]
    assert names == ["files.read", "files.list", "git.git_status"]
    assert "ok: git.git_status" == await multi.call("git.git_status", {})
    assert ".PHONY" in await multi.call("files.read", {"path": str(root / "Makefile")})
    with pytest.raises(LookupError):
        await multi.call("nope.nothing", {})
