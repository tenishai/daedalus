"""Tests for the runtime image's knowledge seeder. Stdlib-only — the techne
write path is injected, so these run in daedalus's dev venv without techne."""

import hashlib
import json
import logging
import shutil
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from seeder import TYPE_MAP, ingest_intake, main, parse_knowledge_file, sync, type_for


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


def test_parse_no_headings_returns_whole_text_stripped(tmp_path):
    """Heading-less input (e.g., intake notes) returns whole text as one section."""
    p = _write(tmp_path, "note.txt", "Just a flat prose note, no ## sections at all.\n")
    assert parse_knowledge_file(p) == ["Just a flat prose note, no ## sections at all."]


def test_parse_empty_or_whitespace_file_returns_empty_list(tmp_path):
    """Empty or whitespace-only file returns empty list."""
    p = _write(tmp_path, "empty.md", "")
    assert parse_knowledge_file(p) == []

    p = _write(tmp_path, "whitespace.md", "   \n\n  \t  \n")
    assert parse_knowledge_file(p) == []


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
        self.tags_by_id = {}  # id -> tags (kept separately so existing 3-tuple
        # unpacking of .rows.values() across the file stays untouched)

    def store(self, db_path, content, type, tags, source):
        i = self.next_id
        self.next_id += 1
        self.rows[i] = (content, type, source)
        self.tags_by_id[i] = tags
        return i

    def delete(self, db_path, entry_id):
        self.tags_by_id.pop(entry_id, None)
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


def _seed_directly(
    fake: FakeStore,
    filename: str,
    sections: list[str],
    type_: str,
    tags: str = "seed",
    source: str | None = None,
) -> list[int]:
    """Simulate the on-disk artifact of a SIGKILL landing between the last
    store_fn() call for a file and the seed_state (or intake_state) commit:
    the entries exist in the store, but no ledger row was ever written for
    the file. Defaults reproduce the original seed-only helper; intake tests
    pass tags="intake" and the sha-scoped source explicitly."""
    source = source or f"seed:{filename}"
    return [fake.store(None, section, type_, tags, source) for section in sections]


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


# --- staleness protocol: seed_state rows the fast lane wrote (lane='live',
# previous_sha, tombstones) must never let a stale image boot destroy or
# resurrect fast-lane memory. These exercise sync()'s branch logic directly
# via hand-written rows -- docker/test_seeder.py stays stdlib-only, so the
# fast lane's own read/write side (apply_knowledge/apply_knowledge_delete)
# is exercised end-to-end instead in tests/test_memory_teach.py, which
# needs techne on the path.


def _seed_state_row(state_db, filename):
    conn = sqlite3.connect(state_db)
    row = conn.execute(
        "SELECT sha256, entry_ids, lane, previous_sha, deleted FROM seed_state "
        "WHERE filename = ?",
        (filename,),
    ).fetchone()
    conn.close()
    return row


def _mark_live(state_db, filename, sha256, previous_sha, entry_ids, deleted=False):
    """Simulate what a fast-lane write (apply_knowledge) or delete
    (apply_knowledge_delete) leaves behind in seed_state, without needing
    techne on the path."""
    conn = sqlite3.connect(state_db)
    conn.execute(
        "UPDATE seed_state SET sha256 = ?, entry_ids = ?, lane = 'live', "
        "previous_sha = ?, deleted = ? WHERE filename = ?",
        (sha256, json.dumps(entry_ids), previous_sha, int(deleted), filename),
    )
    conn.commit()
    conn.close()


def test_live_row_rebuilt_in_is_unchanged_and_flips_lane_to_seed(tmp_path):
    """image_sha == row.sha: the archive commit landed the fast-lane
    write's exact content back into the image. Converged -- treat as
    unchanged and return the row to plain seed lane."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    gotchas_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    gotchas_ids = [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]
    _mark_live(state, "gotchas.md", gotchas_sha, "a" * 64, gotchas_ids)

    before = dict(fake.rows)
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 0, "removed": 0, "unchanged": 2}
    assert fake.rows == before  # nothing stored, nothing deleted

    assert _seed_state_row(state, "gotchas.md") == (
        gotchas_sha, json.dumps(gotchas_ids), "seed", None, 0
    )


def test_live_row_stale_image_is_skipped_entirely(tmp_path):
    """image_sha == row.previous_sha: this boot's image predates the
    fast-lane write. Skip entirely -- no delete, no re-seed. The live
    write's entries stand exactly as they are. This is the flagship
    destructive scenario the staleness protocol exists to prevent."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    # The image on disk still holds the ORIGINAL (pre-edit) bytes -- what a
    # restart before any rebuild would boot against.
    original_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()

    # Simulate the fast lane's own write: new "live" entries replace the
    # seeded ones, sha256 now names content that only exists in techne, not
    # yet on disk.
    for entry_id in [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]:
        fake.delete(None, entry_id)
    live_id = fake.store(None, "## A2\nalpha revised\n", "evaluation", "seed", "seed:gotchas.md")
    live_sha = hashlib.sha256(b"## A2\nalpha revised\n").hexdigest()
    _mark_live(state, "gotchas.md", live_sha, original_sha, [live_id])

    before = dict(fake.rows)
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 0, "removed": 0, "unchanged": 2}
    assert fake.rows == before  # the live entry survives untouched, nothing resurrected

    assert _seed_state_row(state, "gotchas.md") == (
        live_sha, json.dumps([live_id]), "live", original_sha, 0
    )


def test_live_row_genuinely_new_content_converges_normally(tmp_path):
    """image_sha is neither row.sha nor row.previous_sha: a repo-direct edit
    landed, unrelated to the fast-lane write. Normal convergence wins:
    retire the live entries, seed the new content fresh under lane='seed'."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    original_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    for entry_id in [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]:
        fake.delete(None, entry_id)
    live_id = fake.store(None, "## A2\nalpha revised\n", "evaluation", "seed", "seed:gotchas.md")
    live_sha = hashlib.sha256(b"## A2\nalpha revised\n").hexdigest()
    _mark_live(state, "gotchas.md", live_sha, original_sha, [live_id])

    # A THIRD sha appears in the image: a repo-direct edit, unrelated to
    # either the original seed or the fast-lane write.
    (kdir / "gotchas.md").write_text("## A3\nthird version\n")

    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 1, "removed": 0, "unchanged": 1}
    assert live_id not in fake.rows  # the live entry was retired, not left dangling
    assert any(
        c.startswith("## A3") for (c, _, s) in fake.rows.values() if s == "seed:gotchas.md"
    )

    new_sha = hashlib.sha256(b"## A3\nthird version\n").hexdigest()
    row = _seed_state_row(state, "gotchas.md")
    assert row[0] == new_sha
    assert row[2] == "seed"
    assert row[3] is None


def test_tombstone_stale_image_is_skipped_and_tombstone_kept(tmp_path):
    """A fast-lane DELETE leaves a tombstone (lane='live', deleted=1). A
    stale image that still has the pre-delete bytes must not resurrect the
    file: image_sha == previous_sha -> skip, keep the tombstone."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    original_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    for entry_id in [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]:
        fake.delete(None, entry_id)
    _mark_live(state, "gotchas.md", original_sha, original_sha, [], deleted=True)

    before = dict(fake.rows)
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 0, "removed": 0, "unchanged": 2}
    assert not any(s == "seed:gotchas.md" for (_, _, s) in fake.rows.values())
    assert fake.rows == before  # nothing resurrected

    assert _seed_state_row(state, "gotchas.md") == (
        original_sha, "[]", "live", original_sha, 1
    )  # tombstone intact


def test_tombstone_rebuilt_in_drops_the_tombstone(tmp_path):
    """The delete's archive commit removes the file from the image
    entirely. Once the image no longer has the file either, the tombstone
    has converged and can be dropped."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    original_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    for entry_id in [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]:
        fake.delete(None, entry_id)
    _mark_live(state, "gotchas.md", original_sha, original_sha, [], deleted=True)

    (kdir / "gotchas.md").unlink()  # the delete got rebuilt into the image
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 0, "removed": 0, "unchanged": 1}
    assert not any(s == "seed:gotchas.md" for (_, _, s) in fake.rows.values())
    assert _seed_state_row(state, "gotchas.md") is None  # tombstone dropped


def test_tombstone_genuinely_new_content_seeds_fresh(tmp_path):
    """A brand-new file lands at the same filename after a delete (repo-
    direct content, unrelated to the dead version): seed it fresh, same as
    any other new file -- lane='seed', tombstone gone."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    original_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    for entry_id in [i for i, (_, _, s) in fake.rows.items() if s == "seed:gotchas.md"]:
        fake.delete(None, entry_id)
    _mark_live(state, "gotchas.md", original_sha, original_sha, [], deleted=True)

    (kdir / "gotchas.md").write_text("## Fresh\nbrand new content\n")
    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"seeded": 1, "removed": 0, "unchanged": 1}
    assert any(
        c.startswith("## Fresh") for (c, _, s) in fake.rows.values() if s == "seed:gotchas.md"
    )

    new_sha = hashlib.sha256(b"## Fresh\nbrand new content\n").hexdigest()
    row = _seed_state_row(state, "gotchas.md")
    assert row[0] == new_sha
    assert row[2] == "seed"
    assert row[3] is None
    assert row[4] == 0


def test_legacy_seed_state_without_lane_columns_still_converges(tmp_path):
    """A seed_state.db written before the lane/previous_sha/deleted columns
    existed (an existing production PVC) must still work: the migration
    ALTERs the columns in additively, and rows with no lane on record
    behave as lane='seed' -- unchanged semantics, not an error."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"

    gotchas_ids = _seed_directly(
        fake, "gotchas.md", ["## A\nalpha\n", "## B\nbeta\n"], "evaluation"
    )
    facts_ids = _seed_directly(fake, "facts.md", ["## C\ngamma\n"], "fact")

    # Write the OLD-format table by hand: no lane/previous_sha/deleted
    # columns at all, exactly as an existing seed_state.db looks before
    # this change ships.
    conn = sqlite3.connect(state)
    conn.execute(
        "CREATE TABLE seed_state (filename TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
        "entry_ids TEXT NOT NULL)"
    )
    gotchas_sha = hashlib.sha256((kdir / "gotchas.md").read_bytes()).hexdigest()
    facts_sha = hashlib.sha256((kdir / "facts.md").read_bytes()).hexdigest()
    conn.execute(
        "INSERT INTO seed_state (filename, sha256, entry_ids) VALUES (?, ?, ?)",
        ("gotchas.md", gotchas_sha, json.dumps(gotchas_ids)),
    )
    conn.execute(
        "INSERT INTO seed_state (filename, sha256, entry_ids) VALUES (?, ?, ?)",
        ("facts.md", facts_sha, json.dumps(facts_ids)),
    )
    conn.commit()
    conn.close()

    result = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete)

    # Unchanged files (bytes match what the legacy rows recorded) -- no
    # crash, no re-seed, treated as ordinary lane='seed' rows.
    assert result == {"seeded": 0, "removed": 0, "unchanged": 2}
    assert set(fake.rows) == set(gotchas_ids) | set(facts_ids)

    # The migration ran: the columns now exist, and legacy rows read back
    # as lane='seed' with no previous_sha and no tombstone flag.
    conn = sqlite3.connect(state)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seed_state)")}
    assert {"lane", "previous_sha", "deleted"} <= cols
    rows = conn.execute(
        "SELECT filename, lane, previous_sha, deleted FROM seed_state ORDER BY filename"
    ).fetchall()
    conn.close()
    assert rows == [
        ("facts.md", "seed", None, 0),
        ("gotchas.md", "seed", None, 0),
    ]


# --- intake: one-shot ingestion. Opposite lifecycle from knowledge sync --
# ledgered by content-sha (not filename), never removed on convergence, and
# the sha-scoped techne source (`intake:<filename>#<sha12>`) is what makes a
# crash-window rerun distinguishable from legitimate filename reuse.


def _intake_dir(tmp_path: Path, name: str = "notes.md", text: str | None = None) -> Path:
    idir = tmp_path / "intake"
    idir.mkdir(exist_ok=True)
    if text is None:
        text = "## One\nfirst\n\n## Two\nsecond\n"
    (idir / name).write_text(text)
    return idir


def test_intake_first_run_ingests_and_ledgers(tmp_path):
    idir, fake = _intake_dir(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    digest = hashlib.sha256((idir / "notes.md").read_bytes()).hexdigest()

    result = ingest_intake(idir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"ingested": 2, "skipped": 0}
    expected_source = f"intake:notes.md#{digest[:12]}"
    assert len(fake.rows) == 2
    assert all(source == expected_source for (_, _, source) in fake.rows.values())
    assert all(tags == "intake" for tags in fake.tags_by_id.values())

    conn = sqlite3.connect(state)
    row = conn.execute("SELECT sha256, filename, entry_ids FROM intake_state").fetchone()
    conn.close()
    assert row[0] == digest
    assert row[1] == "notes.md"
    assert sorted(json.loads(row[2])) == sorted(fake.rows)


def test_intake_same_sha_skipped_across_runs(tmp_path):
    idir, fake = _intake_dir(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"

    first = ingest_intake(idir, techne_db, state, fake.store, fake.delete)
    before = dict(fake.rows)
    second = ingest_intake(idir, techne_db, state, fake.store, fake.delete)

    assert first == {"ingested": 2, "skipped": 0}
    assert second == {"ingested": 0, "skipped": 1}
    assert fake.rows == before  # nothing re-stored on the unchanged rerun


def test_intake_filename_reuse_new_content_appends(tmp_path):
    idir, fake = _intake_dir(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"

    ingest_intake(idir, techne_db, state, fake.store, fake.delete)
    first_ids = set(fake.rows)

    (idir / "notes.md").write_text("## Three\nthird\n")
    result = ingest_intake(idir, techne_db, state, fake.store, fake.delete)

    assert result == {"ingested": 1, "skipped": 0}
    # Both ingestions' rows survive: filename reuse with new content appends,
    # it never replaces or removes the earlier ingestion's entries.
    assert first_ids <= set(fake.rows)
    assert len(fake.rows) == 3

    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT filename FROM intake_state ORDER BY sha256").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["notes.md", "notes.md"]  # two ledger rows, one per sha


def test_intake_file_deletion_removes_nothing(tmp_path):
    idir, fake = _intake_dir(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"
    delete_calls = []

    def spy_delete(db_path, entry_id):
        delete_calls.append(entry_id)
        return fake.delete(db_path, entry_id)

    ingest_intake(idir, techne_db, state, fake.store, spy_delete)
    rows_before = dict(fake.rows)

    (idir / "notes.md").unlink()
    result = ingest_intake(idir, techne_db, state, fake.store, spy_delete)

    assert result == {"ingested": 0, "skipped": 0}
    assert delete_calls == []  # delete_fn is never invoked by a one-shot pass
    assert fake.rows == rows_before  # memories outlive the file

    conn = sqlite3.connect(state)
    row = conn.execute("SELECT filename FROM intake_state").fetchone()
    conn.close()
    assert row is not None  # ledger entry is untouched


def test_intake_crash_window_converges_without_duplicates(tmp_path):
    idir, fake = _intake_dir(tmp_path), FakeStore()
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"
    digest = hashlib.sha256((idir / "notes.md").read_bytes()).hexdigest()
    source = f"intake:notes.md#{digest[:12]}"

    # A prior boot's store_fn() calls landed, then it died before the
    # intake_state row committed -- no ledger row exists for this sha.
    stale_ids = _seed_directly(
        fake, "notes.md", ["## One\nfirst", "## Two\nsecond"], "fact", tags="intake", source=source
    )

    def list_ids_fn(db_path, src):
        return [i for i, (_, _, s) in fake.rows.items() if s == src]

    result = ingest_intake(
        idir, techne_db, state, fake.store, fake.delete, list_ids_fn=list_ids_fn
    )

    assert result == {"ingested": 2, "skipped": 0}
    assert not (set(stale_ids) & set(fake.rows))  # orphans purged, not duplicated
    assert len(fake.rows) == 2  # one ingestion's worth, total

    conn = sqlite3.connect(state)
    row = conn.execute("SELECT sha256, entry_ids FROM intake_state").fetchone()
    conn.close()
    assert row[0] == digest
    assert sorted(json.loads(row[1])) == sorted(fake.rows)


def test_intake_txt_files_ingest_whole_or_sectioned(tmp_path):
    idir = tmp_path / "intake"
    idir.mkdir()
    (idir / "plain.txt").write_text("Just a flat note, no headings at all.\n")
    (idir / "sectioned.txt").write_text("## Alpha\nfirst\n\n## Beta\nsecond\n")
    fake = FakeStore()
    state = tmp_path / "seed_state.db"

    result = ingest_intake(idir, tmp_path / "techne.db", state, fake.store, fake.delete)

    assert result == {"ingested": 3, "skipped": 0}  # 1 whole-file + 2 sectioned
    contents = sorted(c for (c, _, _) in fake.rows.values())
    assert contents == [
        "## Alpha\nfirst",
        "## Beta\nsecond",
        "Just a flat note, no headings at all.",
    ]


def test_intake_partial_store_failure_orphans_nothing_and_keeps_prior_state(tmp_path):
    """Mid-file store failure leaves no orphaned rows and no intake_state row; rerun ingests cleanly."""
    idir = tmp_path / "intake"
    idir.mkdir()
    # plain.txt (call 1, succeeds) -> sectioned.txt/Alpha (call 2, succeeds)
    # -> sectioned.txt/Beta (call 3, raises)
    (idir / "plain.txt").write_text("Just a note.\n")
    (idir / "sectioned.txt").write_text("## Alpha\nfirst\n\n## Beta\nsecond\n")
    fake = RaisingStore(fail_on_call=3)
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"

    with pytest.raises(RuntimeError):
        ingest_intake(idir, techne_db, state, fake.store, fake.delete)

    # sectioned.txt's already-stored entry (call 2) was cleaned up, not orphaned.
    assert all(s != "intake:sectioned.txt" or not s.startswith("intake:sectioned.txt")
               for (_, _, s) in fake.rows.values()
               if "sectioned" in s)
    # plain.txt, processed earlier in the same run, was stored successfully.
    assert any("plain.txt" in s for (_, _, s) in fake.rows.values())

    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT sha256, filename FROM intake_state").fetchall()
    conn.close()
    # Only plain.txt's state row was committed; sectioned.txt never got one, so
    # a later run can ingest it fresh instead of treating it as done.
    assert len(rows) == 1
    assert rows[0][1] == "plain.txt"

    # Rerun: sectioned.txt should now ingest cleanly
    fake2 = FakeStore()
    result = ingest_intake(idir, techne_db, state, fake2.store, fake2.delete)
    assert result == {"ingested": 2, "skipped": 1}  # plain.txt skipped (already ledgered), sectioned ingests


def test_intake_same_bytes_different_files_ingests_once_skips_second(tmp_path):
    """Two files with identical content: first ingests, second skipped; first ledger row intact."""
    idir = tmp_path / "intake"
    idir.mkdir()
    identical_text = "## Alpha\nfirst\n\n## Beta\nsecond\n"
    (idir / "file1.md").write_text(identical_text)
    (idir / "file2.md").write_text(identical_text)
    fake = FakeStore()
    state = tmp_path / "seed_state.db"
    techne_db = tmp_path / "techne.db"

    result = ingest_intake(idir, techne_db, state, fake.store, fake.delete)

    # One ingestion (2 sections), one skip
    assert result == {"ingested": 2, "skipped": 1}
    assert len(fake.rows) == 2  # Only file1's entries; file2 was skipped

    # Verify ledger: file1 should have a row with the shared digest
    conn = sqlite3.connect(state)
    rows = conn.execute("SELECT sha256, filename, entry_ids FROM intake_state ORDER BY filename").fetchall()
    conn.close()
    assert len(rows) == 1  # Only one ledger row (for file1)
    assert rows[0][1] == "file1.md"
    assert sorted(json.loads(rows[0][2])) == sorted(fake.rows)  # All entries belong to file1


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

    def store(self, db_path, content, type, tags, confidence, source, provenance=None):
        i = self.next_id
        self.next_id += 1
        self.entries[i] = {
            "id": i,
            "content": content,
            "type": type,
            "tags": tags,
            "confidence": confidence,
            "source": source,
            "provenance": provenance,
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


def test_main_logs_intake_counts_only_when_dir_exists(tmp_path, monkeypatch, caplog):
    _install_fake_techne(monkeypatch)
    techne_home = tmp_path / "techne_home"
    monkeypatch.setenv("TECHNE_HOME", str(techne_home))
    root = _bundle_root(tmp_path)

    caplog.set_level(logging.INFO, logger="seeder")
    assert main(["seeder.py", str(root)]) == 0
    assert "intake:" not in caplog.text  # no intake/ dir in this bundle yet

    caplog.clear()
    idir = root / "intake"
    idir.mkdir()
    (idir / "notes.md").write_text("## One\nfirst\n\n## Two\nsecond\n")

    assert main(["seeder.py", str(root)]) == 0
    assert "intake: ingested=2 skipped=0" in caplog.text


def test_knowledge_convergence_ignores_intake_dir(tmp_path, monkeypatch, caplog):
    """Regression: files under intake/ never appear in seed_state or in
    knowledge's seeded/removed/unchanged counts — the two content classes
    have opposite lifecycles and must never blur."""
    fake = _install_fake_techne(monkeypatch)
    techne_home = tmp_path / "techne_home"
    monkeypatch.setenv("TECHNE_HOME", str(techne_home))
    root = _bundle_root(tmp_path)  # knowledge/gotchas.md (2) + facts.md (1)
    idir = root / "intake"
    idir.mkdir()
    (idir / "notes.md").write_text("## One\nfirst\n\n## Two\nsecond\n\n## Three\nthird\n")

    caplog.set_level(logging.INFO, logger="seeder")
    assert main(["seeder.py", str(root)]) == 0

    # Knowledge convergence's counts are unaffected by the 3 intake entries.
    assert "seeded=3 removed=0 unchanged=0" in caplog.text
    assert "intake: ingested=3 skipped=0" in caplog.text

    conn = sqlite3.connect(techne_home / "seed_state.db")
    seed_filenames = [r[0] for r in conn.execute("SELECT filename FROM seed_state")]
    intake_filenames = [r[0] for r in conn.execute("SELECT filename FROM intake_state")]
    conn.close()
    assert sorted(seed_filenames) == ["facts.md", "gotchas.md"]
    assert intake_filenames == ["notes.md"]

    seed_sources = [e["source"] for e in fake.entries.values() if e["source"].startswith("seed:")]
    intake_sources = [
        e["source"] for e in fake.entries.values() if e["source"].startswith("intake:")
    ]
    assert len(seed_sources) == 3
    assert len(intake_sources) == 3


# --- skills seeding (2026-08-27: bundle/skills was dead weight before) --------


def _skills(tmp_path):
    sdir = tmp_path / "skills"
    (sdir / "cost-report").mkdir(parents=True)
    (sdir / "cost-report" / "SKILL.md").write_text(
        "---\nname: cost-report\nversion: 1.2.0\nprofiles: [sre]\n---\n"
        "# Cost report\n\nRun the monthly cost query and post it.\n"
    )
    return sdir


def test_skill_seeds_as_one_procedure_entry_with_frontmatter_stripped(tmp_path):
    from seeder import parse_skill_file

    kdir, fake = _bundle(tmp_path), FakeStore()
    sdir = _skills(tmp_path)
    result = sync(
        kdir, tmp_path / "techne.db", tmp_path / "seed_state.db",
        fake.store, fake.delete, skills_dir=sdir,
    )
    assert result["seeded"] == 4  # 3 knowledge sections + 1 whole-file skill
    skill_rows = [(c, t, s) for c, t, s in fake.rows.values()
                  if s == "seed:skills/cost-report/SKILL.md"]
    assert len(skill_rows) == 1
    content, type_, _ = skill_rows[0]
    assert type_ == "procedure"
    assert content.startswith("## Skill: cost-report")
    assert "Run the monthly cost query" in content
    assert "version:" not in content  # frontmatter stripped
    sections = parse_skill_file(
        "skills/cost-report/SKILL.md", sdir / "cost-report" / "SKILL.md"
    )
    assert len(sections) == 1 and "profiles" not in sections[0]


def test_skill_without_frontmatter_seeds_verbatim_body(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    sdir = tmp_path / "skills"
    (sdir / "plain").mkdir(parents=True)
    (sdir / "plain" / "SKILL.md").write_text("Just do the thing.\n")
    sync(kdir, tmp_path / "techne.db", tmp_path / "seed_state.db",
         fake.store, fake.delete, skills_dir=sdir)
    rows = [c for c, _, s in fake.rows.values() if s == "seed:skills/plain/SKILL.md"]
    assert rows == ["## Skill: plain\n\nJust do the thing."]


def test_skill_change_and_removal_converge_like_knowledge(tmp_path):
    kdir, fake = _bundle(tmp_path), FakeStore()
    sdir = _skills(tmp_path)
    state = tmp_path / "seed_state.db"
    sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete, skills_dir=sdir)

    before = dict(fake.rows)
    r = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete, skills_dir=sdir)
    assert r["seeded"] == 0 and fake.rows == before

    (sdir / "cost-report" / "SKILL.md").write_text("Revised procedure.\n")
    r = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete, skills_dir=sdir)
    assert r["seeded"] == 1
    rows = [c for c, _, s in fake.rows.values()
            if s == "seed:skills/cost-report/SKILL.md"]
    assert rows == ["## Skill: cost-report\n\nRevised procedure."]

    shutil.rmtree(sdir / "cost-report")
    r = sync(kdir, tmp_path / "techne.db", state, fake.store, fake.delete, skills_dir=sdir)
    assert r["removed"] == 1
    assert not [s for _, _, s in fake.rows.values()
                if s == "seed:skills/cost-report/SKILL.md"]


def test_sync_without_skills_dir_is_byte_identical_to_before(tmp_path):
    """The knowledge-only call shape (no skills_dir) behaves exactly as it
    always has -- the parameter is additive."""
    kdir, fake = _bundle(tmp_path), FakeStore()
    result = sync(kdir, tmp_path / "techne.db", tmp_path / "seed_state.db",
                  fake.store, fake.delete)
    assert result == {"seeded": 3, "removed": 0, "unchanged": 0}
