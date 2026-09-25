"""The conversation journal: what the learnings summarizer reads.

Plain sqlite + stdlib -- no techne dependency, so no importorskip here.
"""

from datetime import UTC, datetime, timedelta

from daedalus.memory.journal import journal_path, recent, record

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_journal_path_is_sibling_of_memory_db(tmp_path):
    assert journal_path(tmp_path / "techne.db") == tmp_path / "journal.db"


def test_record_then_recent_roundtrips(tmp_path):
    path = tmp_path / "journal.db"
    history = [{"role": "user", "text": "hi"}, {"role": "agent", "text": "hello"}]
    record(path, "slack:C1:171", history, now=NOW)

    rows = recent(path, now=NOW)

    assert len(rows) == 1
    assert rows[0]["context_id"] == "slack:C1:171"
    assert rows[0]["history"] == history


def test_recent_excludes_rows_older_than_window(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "old", [{"role": "user", "text": "stale"}], now=NOW - timedelta(hours=25))
    record(path, "new", [{"role": "user", "text": "fresh"}], now=NOW - timedelta(hours=1))

    rows = recent(path, now=NOW)

    assert [r["context_id"] for r in rows] == ["new"]


def test_recent_orders_oldest_first(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "first", [{"role": "user", "text": "a"}], now=NOW - timedelta(hours=3))
    record(path, "second", [{"role": "user", "text": "b"}], now=NOW - timedelta(hours=1))

    rows = recent(path, now=NOW)

    assert [r["context_id"] for r in rows] == ["first", "second"]


def test_record_prunes_past_retention(tmp_path):
    path = tmp_path / "journal.db"
    record(path, "ancient", [{"role": "user", "text": "x"}], now=NOW - timedelta(days=8))
    record(path, "today", [{"role": "user", "text": "y"}], now=NOW)

    # The 8-day-old row was pruned by today's write; widen the read window
    # past it to prove deletion rather than mere filtering.
    rows = recent(path, hours=24 * 30, now=NOW)

    assert [r["context_id"] for r in rows] == ["today"]


def test_recent_missing_file_reads_as_empty(tmp_path):
    assert recent(tmp_path / "journal.db", now=NOW) == []


def test_record_upserts_per_context_id(tmp_path):
    # Slack keeps one Task per thread, so Task.history accumulates across
    # turns -- an N-turn thread recording the same context_id N times must
    # leave exactly one row (the latest history), not N growing copies.
    path = tmp_path / "journal.db"
    short = [{"role": "user", "text": "turn one"}]
    long = [
        {"role": "user", "text": "turn one"},
        {"role": "agent", "text": "ack"},
        {"role": "user", "text": "turn two"},
    ]
    record(path, "slack:C1:171", short, now=NOW - timedelta(minutes=5))
    record(path, "slack:C1:171", long, now=NOW)

    rows = recent(path, now=NOW)

    assert len(rows) == 1
    assert rows[0]["context_id"] == "slack:C1:171"
    assert rows[0]["history"] == long
