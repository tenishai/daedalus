"""Tests for the runtime image's knowledge seeder. Stdlib-only — the techne
write path is injected, so these run in daedalus's dev venv without techne."""

import json
import sqlite3
from pathlib import Path

from seeder import TYPE_MAP, parse_knowledge_file, sync, type_for


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
