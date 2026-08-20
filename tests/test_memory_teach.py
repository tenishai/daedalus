import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from daedalus.memory.teach import (
    FILENAME_KEY,
    SYSTEM_TASK_KEY,
    TEACH_MARKER,
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
