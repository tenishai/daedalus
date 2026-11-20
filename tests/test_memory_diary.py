"""A transcript diary as a recall source, end to end (#265): a Daffyne index built from
a transcript, named by the bundle's memory.recall_sources, reached through techne's recall,
and rendered with its date and file anchor. Needs techne and daffyne installed; both are
optional extras, so this file skips without them."""

import json

import pytest

pytest.importorskip("techne.api", reason="needs techne 0.19.1 or later")
pytest.importorskip("daffyne.techne_source", reason="needs the diary extra (daffyne)")

import duckdb  # noqa: E402
import techne.core.recall_sources as rs  # noqa: E402
from daffyne.config import Config as DaffyneConfig  # noqa: E402
from daffyne.scan import scan  # noqa: E402

from daedalus.agent import _provenance  # noqa: E402
from daedalus.memory.techne import TechneMemoryStore  # noqa: E402


class _DaffyneEntryPoint:
    """The entry point daffyne registers, named directly so the test does not depend
    on the test venv's installed-distribution scan."""

    name = "daffyne"

    def load(self):
        from daffyne.techne_source import DaffyneSource

        return DaffyneSource


@pytest.fixture
def home(tmp_path, monkeypatch):
    for attr in ("_LOADED", "_FAILED", "_IN_FLIGHT", "_TEAM", "_TEAM_ERROR"):
        monkeypatch.setattr(rs, attr, {})
    monkeypatch.setattr(rs, "_WARNED", set())
    monkeypatch.setattr(rs, "entry_points", lambda group: [_DaffyneEntryPoint()])
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("standalone: true\n")
    from techne.core.database import init_db

    init_db(tmp_path / "techne.db")
    return tmp_path


def _write_diary(home, monkeypatch):
    """One transcript, indexed the way an operator ships it: db + config on the memory
    volume, DAFFYNE_CONFIG pointing at the config (docker/entrypoint.sh does that)."""
    project = home / "transcripts" / "-home-op-src-widget"
    project.mkdir(parents=True)
    line = {
        "type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "s1",
        "timestamp": "2026-05-06T14:02:11.000Z", "cwd": "/home/op/src/widget",
        "message": {
            "role": "user",
            "content": "we settled it: the widget cache uses request coalescing",
        },
    }
    transcript = project / "s1.jsonl"
    transcript.write_text(json.dumps(line) + "\n")
    db = home / "daffyne.duckdb"
    con = duckdb.connect(str(db))
    scan(con, DaffyneConfig(db=db, roots={"claude-code": [home / "transcripts"]}))
    con.close()
    (home / "daffyne.yaml").write_text(
        f"db: {db}\nroots:\n  claude-code: [{home / 'transcripts'}]\n"
    )
    monkeypatch.setenv("DAFFYNE_CONFIG", str(home / "daffyne.yaml"))
    return transcript


async def test_a_diary_row_reaches_the_agent_with_its_date_and_anchor(home, monkeypatch):
    transcript = _write_diary(home, monkeypatch)
    store = TechneMemoryStore(home=home, recall_sources=["daffyne"])
    await store.commit("the widget team ships on Thursdays")

    hits = await store.recall_hits("widget cache coalescing")

    diary = [h for h in hits if h["source"] == "daffyne"]
    assert diary, hits
    row = diary[0]
    assert "request coalescing" in row["content"]
    assert row["source_url"] == f"file://{transcript}#L1"
    assert row["ts"].startswith("2026-05-06")
    assert _provenance(row) == f"(daffyne, 2026-05-06, file://{transcript}#L1)"
    assert {"content": "the widget team ships on Thursdays", "source": "local"} in hits


async def test_the_diary_is_silent_when_the_bundle_does_not_name_it(home, monkeypatch):
    _write_diary(home, monkeypatch)
    hits = await TechneMemoryStore(home=home).recall_hits("widget cache coalescing")
    assert not any(h["source"] == "daffyne" for h in hits)
