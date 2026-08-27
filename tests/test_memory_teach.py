import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from daedalus.memory.teach import (
    FILENAME_KEY,
    KNOWLEDGE_MARKER,
    SYSTEM_TASK_KEY,
    TEACH_MARKER,
    apply_knowledge,
    apply_knowledge_delete,
    apply_teach,
    is_teach_task,
    teach_authorized,
)
from daedalus.task import VERIFIED_SUB_KEY, Task

techne = pytest.importorskip("techne", reason="techne is not installed")
from techne.core import knowledge  # noqa: E402
from techne.core.database import init_db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_seeder():
    spec = importlib.util.spec_from_file_location(
        "seeder", REPO_ROOT / "docker" / "seeder.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def techne_db(tmp_path):
    db = tmp_path / "techne.db"
    init_db(db).close()
    return db


def _rows(db, source):
    return [
        e
        for e in knowledge.list_entries(db, limit=10_000_000, include_superseded=True)
        if e.get("source") == source
    ]


def _teach_task(
    text="The dog is named Tsuru.", *, sub="service:room", filename="chat-20260820-dog.md"
):
    task = Task.from_text(text)
    task.metadata[SYSTEM_TASK_KEY] = TEACH_MARKER
    task.metadata[FILENAME_KEY] = filename
    if sub is not None:
        task.metadata[VERIFIED_SUB_KEY] = sub
    return task


def test_is_teach_task_only_on_exact_marker():
    assert is_teach_task(_teach_task())
    assert not is_teach_task(Task.from_text("hi"))
    other = Task.from_text("hi")
    other.metadata[SYSTEM_TASK_KEY] = "reboot"
    assert not is_teach_task(other)


def test_teach_authorized_rejects_colleagues_and_absent_sub():
    assert teach_authorized(_teach_task(sub="service:room"))
    assert teach_authorized(_teach_task(sub="kobo-room"))
    assert not teach_authorized(_teach_task(sub="agent:ward"))
    assert not teach_authorized(_teach_task(sub=None))
    empty = _teach_task(sub=None)
    empty.metadata[VERIFIED_SUB_KEY] = ""
    assert not teach_authorized(empty)


def test_apply_teach_stores_fact_with_seeder_provenance(techne_db):
    note = "Taught in chat on 2026-08-20.\n\nThe dog is named Tsuru.\n"
    result = apply_teach(note, "chat-20260820-dog.md", techne_db)
    assert result.stored and not result.already_known and result.error is None
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert result.source == f"intake:chat-20260820-dog.md#{digest[:12]}"
    rows = _rows(techne_db, result.source)
    assert len(rows) == 1
    assert rows[0]["content"] == note.strip()
    assert rows[0]["type"] == "fact"
    assert rows[0]["confidence"] == "high"


def test_apply_teach_writes_intake_ledger_row(techne_db):
    note = "A fact worth keeping.\n"
    result = apply_teach(note, "chat-20260820-fact.md", techne_db)
    state = techne_db.parent / "seed_state.db"
    conn = sqlite3.connect(state)
    row = conn.execute(
        "SELECT sha256, filename, entry_ids FROM intake_state"
    ).fetchone()
    conn.close()
    assert row[0] == hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert row[1] == "chat-20260820-fact.md"
    assert json.loads(row[2]) == result.entry_ids


def test_apply_teach_same_note_twice_is_already_known(techne_db):
    note = "A fact worth keeping.\n"
    first = apply_teach(note, "chat-20260820-fact.md", techne_db)
    second = apply_teach(note, "chat-20260820-fact.md", techne_db)
    assert first.stored and second.already_known and not second.stored
    assert len(_rows(techne_db, first.source)) == len(first.entry_ids)


def test_apply_teach_rejects_bad_filename_and_empty_note(techne_db):
    assert apply_teach("x", "../evil.md", techne_db).error
    assert apply_teach("x", "UPPER.md", techne_db).error
    assert apply_teach("x", "", techne_db).error
    assert apply_teach("   \n", "chat-20260820-ok.md", techne_db).error


def test_seeder_skips_what_teach_ledgered(techne_db, tmp_path):
    """The arc's no-double-ingest guarantee: after a teach, the seeder's
    own ingest over the same bytes is a skip, not a re-ingest."""
    seeder = _load_seeder()
    note = "Taught in chat on 2026-08-20.\n\nThe dog is named Tsuru.\n"
    apply_teach(note, "chat-20260820-dog.md", techne_db)
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / "chat-20260820-dog.md").write_text(note, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(
            db, content, type=type, tags=tags, confidence="high", source=source
        )

    result = seeder.ingest_intake(
        intake, techne_db, techne_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    assert result == {"ingested": 0, "skipped": 1}


@pytest.mark.parametrize(
    "note",
    [
        "Taught in chat on 2026-08-20.\n\nFlat prose, no headings.\n",
        "Preamble line.\n\n## First\nalpha\n\n## Second\nbeta\n",
    ],
)
def test_teach_and_seeder_produce_identical_rows(techne_db, tmp_path, note):
    """Parity by test, not by import: the teach handler's sectioning, typing,
    source, and ledger row must match what the seeder would have produced
    for the same file. Drift fails here."""
    seeder = _load_seeder()
    filename = "chat-20260820-par.md"
    taught = apply_teach(note, filename, techne_db)
    taught_rows = sorted(
        (r["content"], r["type"], r["source"]) for r in _rows(techne_db, taught.source)
    )

    other_db = tmp_path / "other" / "techne.db"
    other_db.parent.mkdir()
    init_db(other_db).close()
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / filename).write_text(note, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(
            db, content, type=type, tags=tags, confidence="high", source=source
        )

    seeder.ingest_intake(
        intake, other_db, other_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    source = f"intake:{filename}#{digest[:12]}"
    seeded_rows = sorted((r["content"], r["type"], r["source"]) for r in _rows(other_db, source))
    assert taught_rows == seeded_rows and taught_rows

    taught_ledger = sqlite3.connect(techne_db.parent / "seed_state.db").execute(
        "SELECT sha256, filename FROM intake_state"
    ).fetchall()
    seeded_ledger = sqlite3.connect(other_db.parent / "seed_state.db").execute(
        "SELECT sha256, filename FROM intake_state"
    ).fetchall()
    assert taught_ledger == seeded_ledger


# ---- knowledge: edit/delete semantics (teach stays append-only, untouched) ----


def test_apply_knowledge_edit_retires_old_fact_and_new_fact_is_recalled(techne_db):
    """The distinguishing knowledge behavior, and the one written first: an
    edit is a REPLACE, not an append. Writing the same filename twice with
    different content must retire the first version's entries -- a live
    recall for the old fact misses and the new fact hits -- unlike teach,
    which never retires anything."""
    from techne.core import knowledge, search

    filename = "chat-20260820-dog.md"
    first = apply_knowledge("The dog is named Tsuru.\n", filename, techne_db)
    assert first.stored and not first.error

    second = apply_knowledge("The dog is named Kestrel.\n", filename, techne_db)
    assert second.stored and not second.error
    assert second.entry_ids != first.entry_ids

    # The old entries are retired -- superseded, never hard-deleted.
    for entry_id in first.entry_ids:
        row = knowledge.get(techne_db, entry_id)
        assert row is not None, "retired entries must never be hard-deleted"
        assert row["superseded_by"] is not None

    # A live recall misses the old fact and hits the new one -- the actual
    # path daedalus.memory.techne.TechneMemoryStore.recall() uses.
    old_hits = search.search(techne_db, "Tsuru", table="knowledge", use_vectors=False)
    new_hits = search.search(techne_db, "Kestrel", table="knowledge", use_vectors=False)
    assert old_hits == []
    assert any("Kestrel" in h["content"] for h in new_hits)

    # The default (live-only) listing agrees.
    live_contents = [e["content"] for e in knowledge.list_entries(techne_db, limit=10_000_000)]
    assert not any("Tsuru" in c for c in live_contents)
    assert any("Kestrel" in c for c in live_contents)

    # The ledger now points filename at the new sha's entries; the stale
    # sha row is gone so a filename never resolves to two ledger rows.
    digest = hashlib.sha256(b"The dog is named Kestrel.\n").hexdigest()
    state = techne_db.parent / "seed_state.db"
    rows = sqlite3.connect(state).execute(
        "SELECT sha256, filename, entry_ids FROM intake_state WHERE filename = ?", (filename,)
    ).fetchall()
    assert rows == [(digest, filename, json.dumps(second.entry_ids))]


def test_apply_knowledge_first_write_matches_seeder_and_ledgers_the_sha(techne_db, tmp_path):
    """Parity claim for apply_knowledge's FIRST write of a file (mirrors
    test_teach_and_seeder_produce_identical_rows): identical content shape,
    written through a different door, must still produce the seeder's exact
    rows and ledger a sha the boot seeder would skip."""
    seeder = _load_seeder()
    note = "Preamble line.\n\n## First\nalpha\n\n## Second\nbeta\n"
    filename = "chat-20260820-know.md"
    result = apply_knowledge(note, filename, techne_db)
    assert result.stored and not result.already_known and result.error is None
    known_rows = sorted(
        (r["content"], r["type"], r["source"]) for r in _rows(techne_db, result.source)
    )

    other_db = tmp_path / "other" / "techne.db"
    other_db.parent.mkdir()
    init_db(other_db).close()
    intake = tmp_path / "intake"
    intake.mkdir()
    (intake / filename).write_text(note, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(
            db, content, type=type, tags=tags, confidence="high", source=source
        )

    seeder.ingest_intake(
        intake, other_db, other_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    digest = hashlib.sha256(note.encode("utf-8")).hexdigest()
    source = f"intake:{filename}#{digest[:12]}"
    seeded_rows = sorted((r["content"], r["type"], r["source"]) for r in _rows(other_db, source))
    assert known_rows == seeded_rows and known_rows

    ledger_row = sqlite3.connect(techne_db.parent / "seed_state.db").execute(
        "SELECT sha256, filename FROM intake_state WHERE sha256 = ?", (digest,)
    ).fetchone()
    assert ledger_row == (digest, filename)


def test_apply_knowledge_writes_intake_ledger_row(techne_db):
    note = "A fact worth keeping.\n"
    result = apply_knowledge(note, "chat-20260820-fact.md", techne_db)
    state = techne_db.parent / "seed_state.db"
    conn = sqlite3.connect(state)
    row = conn.execute(
        "SELECT sha256, filename, entry_ids FROM intake_state WHERE filename = ?",
        ("chat-20260820-fact.md",),
    ).fetchone()
    conn.close()
    assert row[0] == hashlib.sha256(note.encode("utf-8")).hexdigest()
    assert row[1] == "chat-20260820-fact.md"
    assert json.loads(row[2]) == result.entry_ids


def test_apply_knowledge_same_content_twice_is_already_known(techne_db):
    note = "A fact worth keeping.\n"
    first = apply_knowledge(note, "chat-20260820-fact.md", techne_db)
    second = apply_knowledge(note, "chat-20260820-fact.md", techne_db)
    assert first.stored and second.already_known and not second.stored
    assert len(_rows(techne_db, first.source)) == len(first.entry_ids)


def test_apply_knowledge_rejects_bad_filename_and_empty_note(techne_db):
    assert apply_knowledge("x", "../evil.md", techne_db).error
    assert apply_knowledge("x", "UPPER.md", techne_db).error
    assert apply_knowledge("x", "", techne_db).error
    assert apply_knowledge("   \n", "chat-20260820-ok.md", techne_db).error


def test_apply_knowledge_delete_retires_entries_and_removes_ledger_row(techne_db):
    from techne.core import knowledge, search

    filename = "chat-20260820-dog.md"
    note = "The dog is named Tsuru.\n"
    written = apply_knowledge(note, filename, techne_db)
    assert written.stored

    deleted = apply_knowledge_delete(filename, techne_db)
    assert deleted.stored and not deleted.error
    assert sorted(deleted.entry_ids) == sorted(written.entry_ids)

    # Every original entry is retired -- superseded, never hard-deleted.
    for entry_id in written.entry_ids:
        row = knowledge.get(techne_db, entry_id)
        assert row is not None, "a delete must never hard-delete a knowledge row"
        assert row["superseded_by"] is not None

    # A live recall for the deleted fact misses entirely.
    hits = search.search(techne_db, "Tsuru", table="knowledge", use_vectors=False)
    assert hits == []
    live_contents = [e["content"] for e in knowledge.list_entries(techne_db, limit=10_000_000)]
    assert not any("Tsuru" in c for c in live_contents)

    # The ledger row for this filename is gone -- a boot seeder that ever
    # saw the same bytes again would treat them as new, not "already known".
    state = techne_db.parent / "seed_state.db"
    rows = sqlite3.connect(state).execute(
        "SELECT * FROM intake_state WHERE filename = ?", (filename,)
    ).fetchall()
    assert rows == []


def test_apply_knowledge_delete_unknown_filename_is_a_clean_error(techne_db):
    result = apply_knowledge_delete("chat-20260820-never-taught.md", techne_db)
    assert not result.stored
    assert result.error


# ---- knowledge lane vs the boot seeder's OWN ledger (seed_state) ----
#
# bundle/knowledge/*.md is ingested at boot by docker/seeder.py's sync()
# into seed_state, keyed by filename, source `seed:{filename}` -- a
# completely different table from intake_state. A file the fast lane edits
# or deletes is very often one the seeder already seeded at boot (e.g. a
# roster file refreshed over chat). These tests seed through the REAL
# seeder first, exactly as boot does, then exercise the fast lane on top.


def _seed_via_seeder(seeder, techne_db, tmp_path, filename, text):
    """Seed `filename` into `techne_db` via the REAL boot seeder (docker/
    seeder.py's sync()), the way bundle/knowledge/*.md is ingested at boot.
    Returns sync()'s result dict."""
    kdir = tmp_path / "knowledge"
    kdir.mkdir(exist_ok=True)
    (kdir / filename).write_text(text, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(db, content, type=type, tags=tags, confidence="high", source=source)

    return seeder.sync(
        kdir, techne_db, techne_db.parent / "seed_state.db", store_fn, knowledge.delete
    )


def test_apply_knowledge_edit_retires_a_seeded_prior_too(techne_db, tmp_path):
    """The reviewer's probe: a file seeded at boot (seed_state, source
    seed:{filename}) and then edited over the fast lane must have its
    seeded fact retired too, not just whatever intake_state has on record
    (nothing, for a file never previously fast-lane-edited). Before the
    fix, apply_knowledge only ever looked at intake_state, so the seeded
    fact and the newly taught one were BOTH live at once -- a ghost."""
    from techne.core import search

    seeder = _load_seeder()
    filename = "colleagues.md"
    seed_result = _seed_via_seeder(
        seeder, techne_db, tmp_path, filename, "The dog is named Tsuru.\n"
    )
    assert seed_result["seeded"] == 1

    edited = apply_knowledge("The dog is named Kestrel.\n", filename, techne_db)
    assert edited.stored and not edited.error

    old_hits = search.search(techne_db, "Tsuru", table="knowledge", use_vectors=False)
    new_hits = search.search(techne_db, "Kestrel", table="knowledge", use_vectors=False)
    assert old_hits == [], "the seeded prior must miss recall after a fast-lane edit"
    assert any("Kestrel" in h["content"] for h in new_hits)

    live_contents = [e["content"] for e in knowledge.list_entries(techne_db, limit=10_000_000)]
    assert not any("Tsuru" in c for c in live_contents)
    assert any("Kestrel" in c for c in live_contents)


def test_apply_knowledge_delete_retires_a_seeded_prior_too(techne_db, tmp_path):
    """Mirror of the edit probe for delete: a file that was ONLY ever seeded
    (never previously fast-lane-edited) has no intake_state row at all.
    Before the fix, apply_knowledge_delete looked ONLY at intake_state and
    returned a clean 'no knowledge on record' error without touching the
    seeded fact, leaving it recallable forever."""
    from techne.core import search

    seeder = _load_seeder()
    filename = "colleagues.md"
    _seed_via_seeder(seeder, techne_db, tmp_path, filename, "The dog is named Tsuru.\n")

    deleted = apply_knowledge_delete(filename, techne_db)
    assert deleted.stored and not deleted.error

    hits = search.search(techne_db, "Tsuru", table="knowledge", use_vectors=False)
    assert hits == [], "the seeded fact must miss recall after a fast-lane delete"
    live_contents = [e["content"] for e in knowledge.list_entries(techne_db, limit=10_000_000)]
    assert not any("Tsuru" in c for c in live_contents)


def test_seeder_resync_after_fast_lane_edit_is_unchanged_not_reingested(techne_db, tmp_path):
    """After a fast-lane edit to a seeded file, the archive lane eventually
    carries the SAME bytes back into bundle/knowledge/{filename} for the
    next image build. That next boot's seeder must see a sha match and call
    the file unchanged -- never re-ingest a duplicate copy under a fresh
    seed:{filename} source alongside the fast lane's own live entries."""
    seeder = _load_seeder()
    filename = "colleagues.md"
    _seed_via_seeder(seeder, techne_db, tmp_path, filename, "The dog is named Tsuru.\n")

    new_text = "The dog is named Kestrel.\n"
    edited = apply_knowledge(new_text, filename, techne_db)
    assert edited.stored

    # The archive lane later delivers the identical bytes back into
    # knowledge/ for the next image build.
    kdir = tmp_path / "knowledge"
    (kdir / filename).write_text(new_text, encoding="utf-8")

    def store_fn(db, content, type, tags, source):
        return knowledge.store(db, content, type=type, tags=tags, confidence="high", source=source)

    result = seeder.sync(
        kdir, techne_db, techne_db.parent / "seed_state.db", store_fn, knowledge.delete
    )
    assert result == {"seeded": 0, "removed": 0, "unchanged": 1}

    # No duplicate: exactly one live entry carries the new content, none
    # for the old.
    live_contents = [e["content"] for e in knowledge.list_entries(techne_db, limit=10_000_000)]
    assert live_contents.count("The dog is named Kestrel.") == 1
    assert not any("Tsuru" in c for c in live_contents)

    # Ledger is consistent: exactly one seed_state row for the filename,
    # its sha matching the archived (== fast-lane-taught) bytes.
    state = techne_db.parent / "seed_state.db"
    rows = sqlite3.connect(state).execute(
        "SELECT filename, sha256, entry_ids FROM seed_state WHERE filename = ?", (filename,)
    ).fetchall()
    assert len(rows) == 1
    digest = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
    assert rows[0][1] == digest
    assert json.loads(rows[0][2]) == edited.entry_ids


def test_is_teach_task_ignores_the_knowledge_marker():
    """is_teach_task stays an exact match on TEACH_MARKER -- the knowledge
    lane is a distinct marker, not a teach variant."""
    task = Task.from_text("hi")
    task.metadata[SYSTEM_TASK_KEY] = KNOWLEDGE_MARKER
    assert not is_teach_task(task)
