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


async def test_an_interpolated_max_bytes_string_is_a_number(root):
    # ${PRUDENCE_READ_BYTES:-40000} in agent.yaml arrives as "40000"; reads must work.
    (root / "small.txt").write_text("hello\n")
    (root / "big.bin").write_text("x" * 1000)
    files = FilesToolProvider([str(root)], max_bytes="100")
    assert "hello" in await files.call("files.read", {"path": str(root / "small.txt")})
    with pytest.raises(ValueError, match="read limit"):
        await files.call("files.read", {"path": str(root / "big.bin")})


@pytest.mark.parametrize("bad", ["lots", "", "0", -5])
async def test_a_bad_max_bytes_is_refused_at_load(root, bad):
    with pytest.raises(ValueError, match="max_bytes must be"):
        FilesToolProvider([str(root)], max_bytes=bad)


async def test_empty_roots_rejected():
    with pytest.raises(ValueError, match="at least one root"):
        FilesToolProvider([])


async def test_collision_warns_once_not_every_turn(root):
    import warnings

    files_a = FilesToolProvider([str(root)])
    files_b = FilesToolProvider([str(root)])
    multi = MultiToolProvider([files_a, files_b])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await multi.tools()
        await multi.tools()
        await multi.tools()
    dupes = [w for w in caught if "duplicate tool name" in str(w.message)]
    assert len(dupes) == 3  # files.read, .list, .search, once each — not once per turn


async def test_multi_provider_routes_by_listing(root):
    files = FilesToolProvider([str(root)])
    fake = FakeTools([{"name": "git.git_status", "description": "", "parameters": {}}])
    multi = MultiToolProvider([files, fake])
    names = [spec["name"] for spec in await multi.tools()]
    assert names == ["files.read", "files.list", "files.search", "git.git_status"]
    assert "ok: git.git_status" == await multi.call("git.git_status", {})
    assert ".PHONY" in await multi.call("files.read", {"path": str(root / "Makefile")})
    with pytest.raises(LookupError):
        await multi.call("nope.nothing", {})


# Ranged reads (#93). A small model needs a low max_bytes, and large source files are
# normal, so the cap must bound what is returned, not whether the file can be read at all.


@pytest.fixture
def big(root):
    path = root / "big.py"
    path.write_text("".join(f"line {n}\n" for n in range(1, 1001)))  # 8,893 bytes
    return path


async def test_ranged_read_of_a_file_over_the_cap(big):
    files = FilesToolProvider([str(big.parent)], max_bytes=500)
    out = await files.call("files.read", {"path": str(big), "offset": 100, "limit": 3})
    assert out.splitlines() == [
        f"[{big} lines 100-102 of 1000]",
        "100\tline 100",
        "101\tline 101",
        "102\tline 102",
    ]


async def test_oversized_error_names_a_remedy_that_works(big):
    files = FilesToolProvider([str(big.parent)], max_bytes=500)
    with pytest.raises(ValueError, match="1000 lines; read it in parts with offset and limit"):
        await files.call("files.read", {"path": str(big)})
    # Following the advice succeeds — the guard's remedy is not another dead end.
    out = await files.call("files.read", {"path": str(big), "offset": 1, "limit": 20})
    assert out.splitlines()[1] == "1\tline 1"


async def test_models_send_numbers_as_strings(big):
    files = FilesToolProvider([str(big.parent)])
    out = await files.call("files.read", {"path": str(big), "offset": "999", "limit": "5"})
    assert out.splitlines() == [
        f"[{big} lines 999-1000 of 1000]",
        "999\tline 999",
        "1000\tline 1000",
    ]


async def test_limit_alone_starts_at_line_one(big):
    files = FilesToolProvider([str(big.parent)])
    out = await files.call("files.read", {"path": str(big), "limit": "2"})
    assert out.splitlines()[1:] == ["1\tline 1", "2\tline 2"]


async def test_a_range_over_the_cap_suggests_a_limit_that_fits(big):
    files = FilesToolProvider([str(big.parent)], max_bytes=500)
    with pytest.raises(ValueError, match=r"limit counts lines, and (\d+) lines? fit") as err:
        await files.call("files.read", {"path": str(big), "offset": 1, "limit": 900})
    fits = int(err.value.args[0].rsplit("and ", 1)[1].split()[0])
    # The suggested window really fits — the remedy is not another refusal.
    out = await files.call("files.read", {"path": str(big), "offset": 1, "limit": fits})
    assert out.splitlines()[1] == "1\tline 1"


async def test_offset_past_the_end(big):
    files = FilesToolProvider([str(big.parent)])
    with pytest.raises(ValueError, match="has 1000 lines; offset 1001 is past the end"):
        await files.call("files.read", {"path": str(big), "offset": 1001})


@pytest.mark.parametrize("bad", [0, -3, "abc", "1.5"])
async def test_bad_line_arguments_are_refused(big, bad):
    files = FilesToolProvider([str(big.parent)])
    with pytest.raises(ValueError, match="'offset' must be"):
        await files.call("files.read", {"path": str(big), "offset": bad})


async def test_schema_offers_offset_and_limit_on_read_only(root):
    files = FilesToolProvider([str(root)], max_bytes=500)
    specs = {spec["name"]: spec for spec in await files.tools()}
    assert set(specs["files.read"]["parameters"]["properties"]) == {"path", "offset", "limit"}
    assert set(specs["files.list"]["parameters"]["properties"]) == {"path"}
    assert "500 bytes" in specs["files.read"]["description"]


async def test_reading_a_directory_points_at_files_list(root):
    files = FilesToolProvider([str(root)])
    with pytest.raises(IsADirectoryError, match="is a directory; use files.list"):
        await files.call("files.read", {"path": str(root / "sub")})


async def test_a_single_line_over_the_cap_says_so(root):
    (root / "minified.js").write_text("x" * 800 + "\n")
    files = FilesToolProvider([str(root)], max_bytes=500)
    with pytest.raises(ValueError, match="line 1 of .* alone exceeds the 500-byte read limit"):
        await files.call("files.read", {"path": str(root / "minified.js"), "limit": 1})


async def test_line_count_in_the_error_matches_the_remedy_on_cr_only_files(root):
    # Classic-Mac line endings: the advertised count must be the one ranged reads use.
    path = root / "old_mac.txt"
    path.write_bytes(b"a\rb\rc\r")
    files = FilesToolProvider([str(root)], max_bytes=3)
    with pytest.raises(ValueError, match="it has 3 lines;"):
        await files.call("files.read", {"path": str(path)})
    out = await files.call("files.read", {"path": str(path), "offset": 3, "limit": 1})
    assert out.splitlines()[0] == f"[{path} lines 3-3 of 3]"


# Bounded cost (PR #94 review): a refused or ranged read of a huge file must not load
# it. Prudence measured 1.6-2.7 GB and 3-5 s on a 300 MB newline-free file before this.


async def test_huge_newline_free_file_is_refused_without_loading_it(root):
    import tracemalloc

    path = root / "blob.min.js"
    path.write_bytes(b"x" * (5 << 20))  # 5 MB, one line
    files = FilesToolProvider([str(root)], max_bytes=40_000)
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="it has 1 line;"):
            await files.call("files.read", {"path": str(path)})
        with pytest.raises(ValueError, match="alone exceeds"):
            await files.call("files.read", {"path": str(path), "limit": 1})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 << 20, f"peak {peak} bytes; the 5 MB file was materialized"


async def test_a_ranged_read_of_an_empty_file_is_not_a_refusal(root):
    (root / "empty.txt").write_text("")
    files = FilesToolProvider([str(root)])
    out = await files.call("files.read", {"path": str(root / "empty.txt"), "limit": 10})
    assert out == f"[{root / 'empty.txt'} is empty: 0 lines]"


@pytest.mark.parametrize(
    "data",
    [b"a\r\nb\rc\nd", b"\r\r\n\n", b"no newline", b"x\n", b"\r", b"a\r\n" * 3],
)
def test_count_lines_matches_text_mode(tmp_path, data):
    from daedalus.tools.builtin import _count_lines

    path = tmp_path / "f"
    path.write_bytes(data)
    with path.open(errors="replace") as handle:
        assert _count_lines(path) == sum(1 for _ in handle)


def test_count_lines_across_a_chunk_boundary(tmp_path, monkeypatch):
    import daedalus.tools.builtin as builtin

    monkeypatch.setattr(builtin, "_CHUNK", 4)
    path = tmp_path / "f"
    path.write_bytes(b"abc\r\nde\r\r\nf")  # the first \r\n straddles bytes 3-4
    with path.open(errors="replace") as handle:
        assert builtin._count_lines(path) == sum(1 for _ in handle) == 4


# files.search (#96). One search replaces the page-through reads a small model
# otherwise spends its steps on.


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "cli.py").write_text(
        "import os\n\ndef main():\n    return run_init()\n\ndef run_init():\n    pass\n"
    )
    (tmp_path / "pkg" / "notes.md").write_text("run_init is the entry point\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text("run: run_init\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("run_init()\n")
    (tmp_path / "blob.bin").write_bytes(b"run_init\0\x01\x02")
    return tmp_path


async def test_search_returns_absolute_path_line_text(tree):
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": r"def run_init\("})
    assert out == f"{tree / 'pkg' / 'cli.py'}:6: def run_init():"


async def test_search_skips_hidden_dependency_and_binary_files(tree):
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": "run_init"})
    assert ".github" not in out and "node_modules" not in out and "blob.bin" not in out
    assert out.count("\n") == 2  # cli.py:4, cli.py:6, notes.md:1


async def test_search_inside_a_hidden_directory_when_asked(tree):
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": "run_init", "path": str(tree / ".github")})
    assert out == f"{tree / '.github' / 'workflows' / 'test.yml'}:1: run: run_init"


async def test_search_glob_and_ignore_case(tree):
    files = FilesToolProvider([str(tree)])
    out = await files.call(
        "files.search", {"pattern": "RUN_INIT", "glob": "*.md", "ignore_case": "true"}
    )
    assert out == f"{tree / 'pkg' / 'notes.md'}:1: run_init is the entry point"


async def test_invalid_regex_names_the_remedy_and_fixed_works(tree):
    files = FilesToolProvider([str(tree)])
    with pytest.raises(ValueError, match="pass fixed: true"):
        await files.call("files.search", {"pattern": "run_init("})
    out = await files.call("files.search", {"pattern": "run_init(", "fixed": True})
    assert out.splitlines()[0].endswith(":4:     return run_init()")


async def test_search_says_when_it_stops_and_when_nothing_matched(tree):
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": "run_init", "max_results": "1"})
    assert out.splitlines()[-1] == (
        "(showing the first 1 match; there are more; narrow the pattern, path or glob "
        "to see them)"
    )
    # Exactly max_results matches is not "more": no note (Prudence, PR 97 review).
    exact = await files.call(
        "files.search", {"pattern": "def run_init", "path": str(tree / "pkg"), "max_results": "1"}
    )
    assert exact == f"{tree / 'pkg' / 'cli.py'}:6: def run_init():"
    none = await files.call("files.search", {"pattern": "no_such_name"})
    assert none == f"no matches for 'no_such_name' in {tree}"


async def test_search_caps_max_results(root):
    (root / "many.txt").write_text("hit\n" * 500)
    files = FilesToolProvider([str(root)])
    out = await files.call("files.search", {"pattern": "hit", "max_results": 10_000})
    assert len(out.splitlines()) == 201 and "showing the first 200 matches" in out


async def test_search_never_leaves_the_roots(tree):
    outside = tree.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("run_init secret\n")
    os.symlink(outside, tree / "pkg" / "linkdir")
    os.symlink(outside / "secret.txt", tree / "pkg" / "linkfile.txt")
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": "secret"})
    assert out == f"no matches for 'secret' in {tree}"
    with pytest.raises(PermissionError):
        await files.call("files.search", {"pattern": "x", "path": str(outside)})


async def test_search_counts_the_big_files_it_skips(root, monkeypatch):
    import daedalus.tools.builtin as builtin

    monkeypatch.setattr(builtin, "_SEARCH_FILE_BYTES", 10)
    (root / "big.txt").write_text("needle " * 10)
    files = FilesToolProvider([str(root)])
    out = await files.call("files.search", {"pattern": "needle", "path": str(root / "big.txt")})
    assert out.splitlines() == [
        f"no matches for 'needle' in {root / 'big.txt'}",
        "(1 file(s) over 10 bytes were not searched)",
    ]


async def test_a_search_inside_a_hidden_directory_covers_its_hidden_names(tree):
    # Temperance's probe on 7043d7e: .github/.hidden.yml was skipped even though the
    # search started inside .github, contradicting "covers it".
    gh = tree / ".github"
    (gh / ".hidden.yml").write_text("run_init here\n")
    (gh / ".cache").mkdir()
    (gh / ".cache" / "c.txt").write_text("run_init cached\n")
    (gh / "node_modules").mkdir()
    (gh / "node_modules" / "dep.js").write_text("run_init()\n")
    files = FilesToolProvider([str(tree)])
    out = await files.call("files.search", {"pattern": "run_init", "path": str(gh)})
    assert out.splitlines() == [
        f"{gh / '.hidden.yml'}:1: run_init here",  # a folder's files, then its subfolders
        f"{gh / '.cache' / 'c.txt'}:1: run_init cached",
        f"{gh / 'workflows' / 'test.yml'}:1: run: run_init",
    ]


async def test_hidden_coverage_does_not_depend_on_root_order(tmp_path):
    inner = tmp_path / ".b"
    (inner / "x").mkdir(parents=True)
    (inner / "x" / ".h.txt").write_text("needle\n")
    for roots in ([str(inner), str(tmp_path)], [str(tmp_path), str(inner)]):
        files = FilesToolProvider(roots)
        out = await files.call("files.search", {"pattern": "needle", "path": str(inner / "x")})
        assert out == f"{inner / 'x' / '.h.txt'}:1: needle", roots


# ---- roots that serve the host, not a workspace, are refused (#199) ----------


@pytest.mark.parametrize("bad", ["/", "/proc", "/proc/self", "/sys", "/dev", "/run", "/etc"])
def test_system_roots_are_refused_at_load(bad):
    with pytest.raises(ValueError, match="refused"):
        FilesToolProvider(roots=[bad])


def test_home_and_its_ancestors_are_refused(tmp_path, monkeypatch):
    home = tmp_path / "home" / "agent"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for bad in (home, home.parent, tmp_path):
        with pytest.raises(ValueError, match=r"\$HOME"):
            FilesToolProvider(roots=[str(bad)])
    (home / "work").mkdir()
    FilesToolProvider(roots=[str(home / "work")])  # a directory inside $HOME is fine


def test_techne_home_and_inside_it_are_refused(tmp_path, monkeypatch):
    store = tmp_path / "techne"
    (store / "sub").mkdir(parents=True)
    monkeypatch.setenv("TECHNE_HOME", str(store))
    for bad in (store, store / "sub"):
        with pytest.raises(ValueError, match="TECHNE_HOME"):
            FilesToolProvider(roots=[str(bad)])
    FilesToolProvider(roots=[str(tmp_path / "elsewhere")])  # a sibling is fine


def test_a_symlink_to_a_system_dir_is_refused(tmp_path):
    link = tmp_path / "innocent"
    link.symlink_to("/etc")
    with pytest.raises(ValueError, match="system directory"):
        FilesToolProvider(roots=[str(link)])


async def test_a_refused_root_is_a_bundle_error_naming_the_group(tmp_path):
    from daedalus.agent import Agent
    from daedalus.bundle import Bundle, BundleError
    from tests.fakes import ScriptedLLM

    (tmp_path / "agent.yaml").write_text(
        "name: x\nmodel:\n  id: m\ntools:\n  provider: none\n"
        "  builtin:\n    files:\n      roots: ['/']\n  allow: [files.read]\n"
    )
    (tmp_path / "agent-card.json").write_text("{}")
    (tmp_path / "persona.md").write_text("You are x.")
    with pytest.raises(BundleError, match=r"tools\.builtin\.files: root '/' is refused"):
        Agent(Bundle.load(tmp_path), llm=ScriptedLLM("hi"))


# ---- off the event loop, and a wall clock on search (#205) -------------------


async def test_a_slow_files_call_does_not_block_the_event_loop(root, monkeypatch):
    import asyncio
    import time

    provider = FilesToolProvider(roots=[str(root)])

    def slow_search(payload):
        time.sleep(0.3)
        return "done"

    monkeypatch.setattr(provider, "_search", slow_search)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    assert await provider.call("files.search", {"pattern": "x"}) == "done"
    task.cancel()
    assert ticks >= 5, "the loop was blocked during the search"


async def test_search_stops_at_its_wall_clock_and_says_so(root, monkeypatch):
    from daedalus.tools import builtin

    for i in range(3):
        (root / f"f{i}.txt").write_text("needle\n")
    monkeypatch.setattr(builtin, "_SEARCH_SECONDS", -1.0)
    provider = FilesToolProvider(roots=[str(root)])
    out = await provider.call("files.search", {"pattern": "needle"})
    assert "stopped after" in out


async def test_the_wall_clock_bounds_the_directory_walk(root, monkeypatch):
    """PR 219 review: _walk walked the whole root before yielding anything, so the
    deadline never bounded it, and a glob that matched nothing got no note."""
    import os
    import time

    from daedalus.tools import builtin

    for d in ("a", "b", "c", "d"):
        (root / d).mkdir()
        (root / d / "f.txt").write_text("needle\n")
    real_walk = os.walk

    def slow_walk(top, *a, **k):
        for entry in real_walk(top, *a, **k):
            time.sleep(0.2)
            yield entry

    monkeypatch.setattr(builtin.os, "walk", slow_walk)
    monkeypatch.setattr(builtin, "_SEARCH_SECONDS", 0.3)
    provider = FilesToolProvider(roots=[str(root)])
    started = time.monotonic()
    out = await provider.call("files.search", {"pattern": "needle", "glob": "*.md"})
    assert time.monotonic() - started < 0.8
    assert "stopped after" in out and "not an absent one" in out
