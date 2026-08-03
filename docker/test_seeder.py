"""Tests for the runtime image's knowledge seeder. Stdlib-only — the techne
write path is injected, so these run in daedalus's dev venv without techne."""

import json
import logging
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from seeder import TYPE_MAP, main, parse_knowledge_file, sync, type_for


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_parse_splits_on_h2_sections(tmp_path):
    p = _write(
        tmp_path,
        "gotchas.md",
        "## First gotcha\nBody one.\n\n## Second gotcha\nBody two.\n",
    )
    sections = parse_knowledge_file(p)
    assert sections == [
        "## First gotcha\nBody one.",
        "## Second gotcha\nBody two.",
    ]


def test_parse_ignores_preamble_and_blank_sections(tmp_path):
    p = _write(tmp_path, "facts.md", "Intro prose, no heading.\n\n## Kept\nYes.\n\n## \n\n")
    assert parse_knowledge_file(p) == ["## Kept\nYes."]


def test_type_for_maps_filename_stems():
    assert type_for("gotchas.md") == "evaluation"
    assert type_for("operational-playbooks.md") == "procedure"
    assert type_for("playbooks.md") == "procedure"
    assert type_for("decisions.md") == "decision"
    assert type_for("team-directory.md") == "fact"  # default
    assert set(TYPE_MAP.values()) <= {"fact", "decision", "evaluation", "procedure"}


class FakeStore:
    """Records store/delete calls the way techne.knowledge would see them."""

    def __init__(self):
        self.next_id = 1
        self.rows = {}  # id -> (content, type, source)

    def store(self, db_path, content, type, tags, source):
        i = self.next_id
        self.next_id += 1
        self.rows[i] = (content, type, source)
        return i

    def delete(self, db_path, entry_id):
        return self.rows.pop(entry_id, None) is not None


def _bundle(tmp_path):
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "gotchas.md").write_text("## A\nalpha\n\n## B\nbeta\n")
    (kdir / "facts.md").write_text("## C\ngamma\n")
    return kdir


def test_first_sync_seeds_everything(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}
    types = sorted(t for (_, t, _) in fake.rows.values())
    assert types == ["evaluation", "evaluation", "fact"]
    assert all(src in ("seed:gotchas.md", "seed:facts.md")
               for (_, _, src) in fake.rows.values())


def test_unchanged_file_is_skipped(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    before = dict(fake.rows)
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["seeded"] == 0 and result["unchanged"] == 2
    assert fake.rows == before


def test_changed_file_replaces_only_its_entries(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    facts_ids = {i for i, (_, _, s) in fake.rows.items() if s == "seed:facts.md"}
    (kdir / "gotchas.md").write_text("## A2\nalpha revised\n")
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["seeded"] == 1
    assert facts_ids <= set(fake.rows)  # facts.md untouched
    assert all(s != "seed:gotchas.md" or c.startswith("## A2")
               for c, _, s in fake.rows.values())


def test_removed_file_drops_its_entries(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    (kdir / "facts.md").unlink()
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    assert result["removed"] == 1
    assert all(s != "seed:facts.md" for (_, _, s) in fake.rows.values())


def test_state_survives_in_sqlite(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)
    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT filename, entry_ids FROM seed_state ORDER BY filename").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["facts.md", "gotchas.md"]
    assert all(isinstance(json.loads(r[1]), list) for r in rows)


class RaisingStore(FakeStore):
    """Like FakeStore, but raises on its Nth store() call (1-indexed)."""

    def __init__(self, fail_on_call: int):
        super().__init__()
        self.fail_on_call = fail_on_call
        self.calls = 0

    def store(self, db_path, content, type, tags, source):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("boom")
        return super().store(db_path, content, type, tags, source)


def test_partial_store_failure_orphans_nothing_and_keeps_prior_file_state(tmp_path):
    # facts.md sorts before gotchas.md, so processing order is:
    #   facts.md/C (call 1, succeeds) -> gotchas.md/A (call 2, succeeds)
    #   -> gotchas.md/B (call 3, raises)
    kdir = _bundle(tmp_path)
    fake = RaisingStore(fail_on_call=3)
    state = tmp_path / "seed_state.db"

    with pytest.raises(RuntimeError):
        sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    # gotchas.md's already-stored entry (call 2) was cleaned up, not orphaned.
    assert all(s != "seed:gotchas.md" for (_, _, s) in fake.rows.values())
    # facts.md, processed earlier in the same run, was stored successfully.
    assert any(s == "seed:facts.md" for (_, _, s) in fake.rows.values())

    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT filename FROM seed_state").fetchall()
    conn.close()
    # Only facts.md's state row was committed; gotchas.md never got one, so
    # a later run can seed it fresh instead of treating it as done.
    assert [r[0] for r in rows] == ["facts.md"]


def test_corrupt_state_db_is_quarantined_purged_and_reseeded(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    state.write_bytes(b"not a sqlite database, just garbage bytes to trigger corruption")

    purge_calls = []
    result = sync(
        kdir,
        tmp_path / "techne.db",
        state,
        fake.store,
        fake.delete,
        purge_fn=lambda db_path: purge_calls.append(db_path),
    )

    assert Path(str(state) + ".corrupt").exists()
    assert len(purge_calls) == 1
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}
    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT filename FROM seed_state ORDER BY filename").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["facts.md", "gotchas.md"]


def test_purge_fn_not_called_when_state_db_is_healthy(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    purge_calls = []

    sync(
        kdir,
        tmp_path / "techne.db",
        state,
        fake.store,
        fake.delete,
        purge_fn=lambda db_path: purge_calls.append(db_path),
    )

    assert purge_calls == []


def _seed_directly(fake: FakeStore, filename: str, sections: list[str], type_: str) -> list[int]:
    """Simulate the on-disk artifact of a SIGKILL landing between the last
    store_fn() call for a file and the seed_state commit: the entries exist
    in the store, but no seed_state row was ever written for the file."""
    return [fake.store(None, section, type_, "seed", f"seed:{filename}") for section in sections]


def test_sigkill_aftermath_converges_via_list_ids_fn(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"

    # gotchas.md's entries already landed in the store from a run that died
    # before the state-row commit, so there is no seed_state row for it.
    stale_ids = _seed_directly(fake, "gotchas.md", ["## A\nalpha\n", "## B\nbeta\n"], "evaluation")
    assert set(fake.rows) == set(stale_ids)

    def list_ids_fn(db_path, source):
        return [i for i, (_, _, s) in fake.rows.items() if s == source]

    result = sync(
        kdir,
        tmp_path / "techne.db",
        state,
        fake.store,
        fake.delete,
        list_ids_fn=list_ids_fn,
    )

    # The state-less stale entries were purged before reseeding — no
    # duplicates survive the restart.
    assert not (set(stale_ids) & set(fake.rows))
    gotchas_rows = [r for r in fake.rows.values() if r[2] == "seed:gotchas.md"]
    assert len(gotchas_rows) == 2
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}


def test_without_list_ids_fn_behavior_is_unchanged(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"

    stale_ids = _seed_directly(fake, "gotchas.md", ["## A\nalpha\n", "## B\nbeta\n"], "evaluation")

    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    # No list_ids_fn means no ground-truth convergence sweep: the stale,
    # state-less entries from the "lost" run are left untouched and sync
    # stores fresh ones alongside them — the pre-existing behavior.
    assert set(stale_ids) <= set(fake.rows)
    gotchas_rows = [r for r in fake.rows.values() if r[2] == "seed:gotchas.md"]
    assert len(gotchas_rows) == 4
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}


def test_corrupt_state_with_purge_and_list_ids_fn_converges(tmp_path):
    """Corrupt state + both purge_fn and list_ids_fn: quarantine, purge, converge, no duplicates."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"

    # Write garbage to corrupt the state db.
    state.write_bytes(b"garbage bytes for corruption test")

    # Pre-seed stale entries (simulating entries from a run killed before state commit).
    _seed_directly(fake, "gotchas.md", ["## A\nalpha\n", "## B\nbeta\n"], "evaluation")
    _seed_directly(fake, "facts.md", ["## C\ngamma\n"], "fact")

    # Track purge_fn calls.
    purge_calls = []

    # Track list_ids_fn calls and what it found.
    list_ids_calls = []

    # purge_fn that actually deletes seed-sourced entries from FakeStore.
    def purge_fn(db_path):
        purge_calls.append(db_path)
        for i in list(fake.rows.keys()):
            _, _, source = fake.rows[i]
            if source.startswith("seed:"):
                fake.delete(db_path, i)

    # list_ids_fn that returns ids by exact source match.
    def list_ids_fn(db_path, source):
        ids = [i for i, (_, _, s) in fake.rows.items() if s == source]
        list_ids_calls.append((source, ids))
        return ids

    result = sync(
        kdir,
        tmp_path / "techne.db",
        state,
        fake.store,
        fake.delete,
        purge_fn=purge_fn,
        list_ids_fn=list_ids_fn,
    )

    # Assertion 1: quarantine file exists.
    assert Path(str(state) + ".corrupt").exists()

    # Assertion 2: purge called exactly once.
    assert len(purge_calls) == 1

    # Assertion 3: reseed stored each section exactly once (no duplicates)
    # and result counts are correct.
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}

    # Count entries per source to verify no duplicates.
    sources_count = {}
    for _, _, source in fake.rows.values():
        sources_count[source] = sources_count.get(source, 0) + 1
    assert sources_count.get("seed:gotchas.md", 0) == 2
    assert sources_count.get("seed:facts.md", 0) == 1

    # Assertion 4: list_ids_fn (when consulted post-purge) found nothing.
    # After purge_fn runs, all seed-sourced entries are gone,
    # so list_ids_fn should return empty ids for every source it checks.
    assert len(list_ids_calls) == 2  # Called once per file during convergence.
    assert all(ids == [] for _, ids in list_ids_calls)


# --- main()'s last-file bug: git drops empty dirs, so deleting the LAST
# knowledge file removes knowledge/ from the built image entirely (not
# merely empties it). main() must still converge in that case instead of
# early-returning before sync() ever runs. sync() itself already tolerates
# a missing knowledge_dir (Path.glob on a nonexistent dir yields nothing,
# same as an empty one) — these tests exercise main()'s control flow, so
# they fake techne.core.{knowledge,database} the way FakeStore fakes the
# store/delete path for sync() above, and inject them via sys.modules since
# main()'s `from techne.core import knowledge` import is function-local.


class FakeKnowledgeModule:
    """Fakes techne.core.knowledge's API surface (store/delete/list_entries)
    for testing main() end-to-end without techne installed."""

    def __init__(self):
        self.next_id = 1
        self.entries = {}  # id -> dict(content, type, tags, confidence, source)

    def store(self, db_path, content, type, tags, confidence, source):
        i = self.next_id
        self.next_id += 1
        self.entries[i] = {
            "id": i,
            "content": content,
            "type": type,
            "tags": tags,
            "confidence": confidence,
            "source": source,
        }
        return i

    def delete(self, db_path, entry_id):
        return self.entries.pop(entry_id, None) is not None

    def list_entries(self, db_path, limit=1000, include_superseded=False):
        return list(self.entries.values())


def _install_fake_techne(monkeypatch) -> FakeKnowledgeModule:
    fake_knowledge = FakeKnowledgeModule()

    knowledge_mod = types.ModuleType("techne.core.knowledge")
    knowledge_mod.store = fake_knowledge.store
    knowledge_mod.delete = fake_knowledge.delete
    knowledge_mod.list_entries = fake_knowledge.list_entries

    class _Conn:
        def close(self):
            pass

    database_mod = types.ModuleType("techne.core.database")
    database_mod.init_db = lambda db_path: _Conn()

    core_mod = types.ModuleType("techne.core")
    core_mod.knowledge = knowledge_mod
    core_mod.database = database_mod
    techne_mod = types.ModuleType("techne")
    techne_mod.core = core_mod

    monkeypatch.setitem(sys.modules, "techne", techne_mod)
    monkeypatch.setitem(sys.modules, "techne.core", core_mod)
    monkeypatch.setitem(sys.modules, "techne.core.knowledge", knowledge_mod)
    monkeypatch.setitem(sys.modules, "techne.core.database", database_mod)
    return fake_knowledge


def _bundle_root(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    kdir = root / "knowledge"
    kdir.mkdir(parents=True)
    (kdir / "gotchas.md").write_text("## A\nalpha\n\n## B\nbeta\n")
    (kdir / "facts.md").write_text("## C\ngamma\n")
    return root


def test_main_purges_state_when_last_knowledge_file_removal_drops_the_dir(
    tmp_path, monkeypatch, caplog
):
    fake = _install_fake_techne(monkeypatch)
    techne_home = tmp_path / "techne_home"
    monkeypatch.setenv("TECHNE_HOME", str(techne_home))
    root = _bundle_root(tmp_path)

    assert main(["seeder.py", str(root)]) == 0
    assert len(fake.entries) == 3  # gotchas.md's 2 sections + facts.md's 1

    # Deleting the last file(s) drops knowledge/ from the image entirely —
    # git does not track empty directories.
    shutil.rmtree(root / "knowledge")

    caplog.set_level(logging.INFO, logger="seeder")
    assert main(["seeder.py", str(root)]) == 0

    assert fake.entries == {}  # both files' entries dropped via delete_fn
    conn = sqlite3.connect(techne_home / "seed_state.db")
    rows = conn.execute("SELECT filename FROM seed_state").fetchall()
    conn.close()
    assert rows == []  # their state rows are gone too
    assert "seeded=0 removed=2 unchanged=0" in caplog.text


def test_main_never_seeded_bundle_with_no_knowledge_dir_is_a_clean_noop(
    tmp_path, monkeypatch, caplog
):
    fake = _install_fake_techne(monkeypatch)
    techne_home = tmp_path / "techne_home"
    monkeypatch.setenv("TECHNE_HOME", str(techne_home))
    root = tmp_path / "bundle"
    root.mkdir()  # no knowledge/ subdir at all — this bundle never had one

    caplog.set_level(logging.INFO, logger="seeder")
    assert main(["seeder.py", str(root)]) == 0

    assert fake.entries == {}
    state_db = techne_home / "seed_state.db"
    if state_db.exists():
        conn = sqlite3.connect(state_db)
        rows = conn.execute("SELECT filename FROM seed_state").fetchall()
        conn.close()
        assert rows == []
    assert "nothing to seed" in caplog.text
