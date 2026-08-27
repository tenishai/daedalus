"""Convergent knowledge seeder for the daedalus runtime image.

Seeds bundle/knowledge/*.md into a techne SQLite store at boot. Convergent
per file: only files whose content hash changed are re-seeded; entries from
removed files are dropped; learned (non-seed) entries are never touched.

Section convention: knowledge files are split on `## ` headings; the filename
stem picks the techne knowledge type (see TYPE_MAP; anything else is a fact).

bundle/skills/<id>/SKILL.md rides the SAME convergence pass (2026-08-27:
until then the runtime never read bundle/skills at all -- kobo staged skill
picks into every image and every one of them was dead weight). Each SKILL.md
enters `sync()`'s ledger under the collision-free key `skills/<id>/SKILL.md`
and seeds as ONE whole-file `procedure` entry -- a skill is a coherent
procedure, and splitting it on `## ` headings the way knowledge files split
would fragment the very steps that make it one. Any leading YAML
frontmatter (the marketplace's version/profiles block) is stripped: it is
catalog metadata, not procedure. A skill's support files beyond SKILL.md
are not seeded (nothing consumes them yet); the SKILL.md is the directive.

bundle/intake/*.{md,txt} gets the opposite lifecycle: a one-shot append pass
(see ingest_intake) ledgered by content-sha rather than filename. Once a
file's sha is ledgered it is never touched again — not on content change
(new sha, new ingestion), not on deletion (memories outlive the file). Never
part of knowledge convergence: intake rows never appear in seed_state and
are never removed or counted by sync().

Staleness protocol: bundle/knowledge/*.md can also be edited or deleted live,
outside this seeder entirely, over daedalus's fast lane (see
src/daedalus/memory/teach.py's apply_knowledge/apply_knowledge_delete). That
lane stamps its seed_state row with lane='live' and previous_sha=<the sha the
row held before its write, chained back to the image-era sha through any run
of live writes with no rebuild between them>; a fast-lane delete leaves a
TOMBSTONE row (lane='live', deleted=1) instead of dropping the row outright.
Both exist so this seeder, which runs on every boot against whatever bytes
happen to be baked into the image, can tell a genuinely new repo-direct edit
apart from an image that simply predates the live write reaching a rebuild:
matching the image's sha against sha256 (rebuilt in -- converged, return to
plain seed lane) before falling back to previous_sha (stale image -- skip
entirely, never delete, never re-seed) is what stops a stale-image boot from
silently reverting a taught edit or resurrecting a fast-lane delete.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

TYPE_MAP = {
    "gotchas": "evaluation",
    "playbooks": "procedure",
    "operational-playbooks": "procedure",
    "decisions": "decision",
}

_H2 = re.compile(r"^## ", re.MULTILINE)


_SKILL_KEY_PREFIX = "skills/"

# Leading YAML frontmatter block (marketplace SKILL.md dialect: version,
# profiles, owner...). Catalog metadata, never procedure -- stripped before
# seeding. DOTALL so the block may span lines; anchored at the very start.
_FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def type_for(filename: str) -> str:
    if filename.startswith(_SKILL_KEY_PREFIX):
        return "procedure"
    return TYPE_MAP.get(Path(filename).stem, "fact")


def parse_skill_file(key: str, path: Path) -> list[str]:
    """One whole-file procedure section per SKILL.md (see the module
    docstring for why skills are never split on headings the way
    knowledge files are). The section leads with a heading naming the
    skill id -- the bundle directory name, taken from `key`'s middle
    segment -- so the seeded entry self-describes in recall the same way
    a knowledge file's own `## ` heading does."""
    text = _FRONTMATTER.sub("", path.read_text(), count=1).strip()
    if not text:
        return []
    skill_id = key.split("/")[1]
    return [f"## Skill: {skill_id}\n\n{text}"]


def parse_knowledge_file(path: Path) -> list[str]:
    text = path.read_text()
    starts = [m.start() for m in _H2.finditer(text)]
    if not starts:
        # No `## ` headings at all: intake notes are frequently flat prose
        # rather than knowledge-file-style sections, so the whole (trimmed)
        # file becomes its one section instead of yielding nothing.
        whole = text.strip()
        return [whole] if whole else []
    sections = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        section = text[start:end].strip()
        if section.removeprefix("##").strip():
            sections.append(section)
    return sections


log = logging.getLogger("seeder")

_SEED_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS seed_state (
       filename TEXT PRIMARY KEY,
       sha256 TEXT NOT NULL,
       entry_ids TEXT NOT NULL,
       lane TEXT NOT NULL DEFAULT 'seed',
       previous_sha TEXT,
       deleted INTEGER NOT NULL DEFAULT 0
   )"""

_INTAKE_STATE_SCHEMA = """CREATE TABLE IF NOT EXISTS intake_state (
       sha256 TEXT PRIMARY KEY,
       filename TEXT NOT NULL,
       entry_ids TEXT NOT NULL
   )"""


def _migrate_seed_state_schema(conn: sqlite3.Connection) -> None:
    """Additive ALTERs for a seed_state.db written before the staleness
    protocol's columns existed. The CREATE TABLE IF NOT EXISTS above already
    includes them for a brand-new db; this only ever fires against an
    existing table from before this change (a live PVC's seed_state.db
    predating this ship), so it must be ALTER-safe rather than destructive.
    Existing rows read back as lane='seed', previous_sha=NULL, deleted=0 --
    the same as if the columns had always been there and every prior write
    had been a plain seed (unchanged legacy semantics)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seed_state)")}
    if "lane" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN lane TEXT NOT NULL DEFAULT 'seed'")
    if "previous_sha" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN previous_sha TEXT")
    if "deleted" not in cols:
        conn.execute("ALTER TABLE seed_state ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _state_conn(state_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(state_db)
    try:
        conn.execute(_SEED_STATE_SCHEMA)
        conn.execute(_INTAKE_STATE_SCHEMA)
        _migrate_seed_state_schema(conn)
    except sqlite3.DatabaseError:
        # Corrupt file (e.g. an unclean kill of a persistent volume left a
        # malformed sqlite image) — close and let the caller quarantine it.
        conn.close()
        raise
    return conn


def _load_state(state_db: Path) -> tuple[sqlite3.Connection, dict]:
    """Open the state db and read its rows.

    Raises sqlite3.DatabaseError if the on-disk file is corrupt; the caller
    is responsible for quarantining and rebuilding in that case.
    """
    conn = _state_conn(state_db)
    try:
        state = {
            row[0]: {
                "sha256": row[1],
                "entry_ids": json.loads(row[2]),
                "lane": row[3] or "seed",
                "previous_sha": row[4],
                "deleted": bool(row[5]),
            }
            for row in conn.execute(
                "SELECT filename, sha256, entry_ids, lane, previous_sha, deleted FROM seed_state"
            )
        }
    except sqlite3.DatabaseError:
        conn.close()
        raise
    return conn, state


def sync(
    knowledge_dir: Path,
    techne_db: Path,
    state_db: Path,
    store_fn,
    delete_fn,
    purge_fn=None,
    list_ids_fn: Callable[[Path, str], list[int]] | None = None,
    retire_fn: Callable[[Path, list[int], int], None] | None = None,
    skills_dir: Path | None = None,
) -> dict:
    try:
        conn, state = _load_state(state_db)
    except sqlite3.DatabaseError:
        log.warning("state db %s is corrupt — quarantining and rebuilding", state_db)
        if purge_fn is not None:
            # Rebuilding from empty state would re-seed everything as new
            # entries, duplicating whatever was already stored under the
            # lost state — purge the prior seed-sourced entries first.
            # Run before the quarantine rename: purge is idempotent, so if
            # it raises mid-way, recovery is retryable on the next run
            # (the corrupt state db is still in place to retry against)
            # instead of leaving duplicated entries with no way back in.
            purge_fn(techne_db)
        os.replace(state_db, str(state_db) + ".corrupt")
        conn, state = _load_state(state_db)

    try:
        current = {p.name: p for p in sorted(knowledge_dir.glob("*.md"))}
        if skills_dir is not None:
            # Same ledger, same convergence, same staleness rules -- one
            # removal pass over one `current` map is load-bearing: a second
            # sync() call against the same state_db would see the other
            # call's rows as removed files and delete their entries.
            # Key shape `skills/<id>/SKILL.md` cannot collide with a flat
            # knowledge filename.
            for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
                current[f"skills/{skill_md.parent.name}/SKILL.md"] = skill_md
        seeded = removed = unchanged = 0

        for filename in set(state) - set(current):
            row = state[filename]
            if row["lane"] == "live" and row["deleted"]:
                # A fast-lane delete's tombstone, and the image no longer
                # has the file at all: the delete got rebuilt in. There is
                # nothing live to retire (a tombstone's entries were
                # already retired at delete time -- see docker/seeder.py's
                # sibling, src/daedalus/memory/teach.py's
                # apply_knowledge_delete), so just drop the now-redundant
                # ledger row. Not counted as "removed": nothing was.
                conn.execute("DELETE FROM seed_state WHERE filename = ?", (filename,))
                conn.commit()
                continue
            for entry_id in row["entry_ids"]:
                delete_fn(techne_db, entry_id)
            conn.execute("DELETE FROM seed_state WHERE filename = ?", (filename,))
            conn.commit()
            removed += 1

        def _store_sections(filename: str, path: Path) -> list[int]:
            """Store `path`'s sections fresh under the ordinary seed source.
            Exception cleanup only; the caller decides what to do with the
            old entries (if any) and writes the ledger row."""
            sections = (
                parse_skill_file(filename, path)
                if filename.startswith(_SKILL_KEY_PREFIX)
                else parse_knowledge_file(path)
            )
            ids: list[int] = []
            try:
                for section in sections:
                    ids.append(
                        store_fn(techne_db, section, type_for(filename), "seed", f"seed:{filename}")
                    )
            except Exception:
                # Best-effort cleanup so a mid-file store failure never
                # orphans entries the state db has no record of.
                for entry_id in ids:
                    try:
                        delete_fn(techne_db, entry_id)
                    except Exception:
                        log.warning(
                            "cleanup failed for orphaned entry %s from %s", entry_id, filename
                        )
                raise
            return ids

        def _write_seed_row(filename: str, digest: str, ids: list[int]) -> None:
            conn.execute(
                "INSERT OR REPLACE INTO seed_state "
                "(filename, sha256, entry_ids, lane, previous_sha, deleted) "
                "VALUES (?, ?, ?, 'seed', NULL, 0)",
                (filename, digest, json.dumps(ids)),
            )
            conn.commit()

        def _reseed(filename: str, path: Path, digest: str) -> int:
            """Store `path`'s sections fresh and (re)write its seed_state
            row as an ordinary lane='seed' entry -- the shared tail of
            every "there is new content to seed" branch that has no old
            fast-lane survivor entries to worry about (a brand new
            filename, an ordinary changed seed file, or a tombstone
            converging with genuinely new content -- a tombstone's own
            entries are already empty, retired at delete time)."""
            ids = _store_sections(filename, path)
            _write_seed_row(filename, digest, ids)
            return len(ids)

        def _retire_or_delete(entry_ids: list[int], survivor_id: int) -> None:
            """Remove `entry_ids`, preferring retire_fn (repoint
            superseded_by at `survivor_id`) over a hard delete_fn call.
            This matters specifically for a lane='live' row's entries:
            the fast lane's own apply_knowledge always retires whatever a
            file's prior seed/intake entries were INTO its freshly-stored
            entry as survivor (see src/daedalus/memory/teach.py), so a
            lane='live' row's entry is very often itself already a
            retirement survivor that something else's superseded_by points
            at. Hard-deleting it violates techne's own foreign key on
            superseded_by; retiring it into the newly-seeded survivor
            instead is always safe, referenced or not, and mirrors exactly
            what the fast lane does for its own edits. Callers without a
            retire_fn (e.g. the stdlib-only unit tests in
            docker/test_seeder.py, whose FakeStore has no such constraint)
            fall back to the original hard-delete behavior."""
            if retire_fn is not None:
                if entry_ids:
                    retire_fn(techne_db, entry_ids, survivor_id)
            else:
                for entry_id in entry_ids:
                    delete_fn(techne_db, entry_id)

        for filename, path in current.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            prior = state.get(filename)

            if prior is None:
                if list_ids_fn is not None:
                    # Ground-truth convergence: no state row for this
                    # filename means either it's genuinely new, or a
                    # SIGKILL previously landed between a store_fn() call
                    # and the state-row commit below — no exception was
                    # raised, so the except-block cleanup never ran, and no
                    # state row was written to remember the entries either.
                    # Either way techne itself is the ground truth, so drop
                    # anything already sourced from this exact file before
                    # storing fresh entries — a restart can then never
                    # leave duplicates behind.
                    for entry_id in list_ids_fn(techne_db, f"seed:{filename}"):
                        delete_fn(techne_db, entry_id)
                seeded += _reseed(filename, path, digest)
                continue

            if prior["lane"] == "live":
                if prior["deleted"]:
                    if digest == prior["previous_sha"]:
                        # STALE image of a deleted file: this boot's image
                        # predates the fast-lane delete. Skip entirely --
                        # keep the tombstone, never resurrect the file.
                        unchanged += 1
                        continue
                    # Else: genuinely new content landed at this filename
                    # after the delete (a repo-direct add, unrelated to the
                    # dead version) -- falls through to the generic reseed
                    # below, same as any other new file. Nothing to
                    # delete_fn: the tombstone's entries were already
                    # retired at delete time.
                    seeded += _reseed(filename, path, digest)
                    continue
                if digest == prior["sha256"]:
                    # The archive commit landed the fast-lane write's exact
                    # content back into the image: converged. Return the
                    # row to plain seed lane so a FUTURE repo-direct edit
                    # is ordinary convergence, not staleness logic again.
                    conn.execute(
                        "UPDATE seed_state SET lane = 'seed', previous_sha = NULL "
                        "WHERE filename = ?",
                        (filename,),
                    )
                    conn.commit()
                    unchanged += 1
                    continue
                if prior["previous_sha"] is not None and digest == prior["previous_sha"]:
                    # STALE image: this boot's image predates the
                    # fast-lane write entirely. Skip entirely -- no
                    # delete, no re-seed. The live write's entries stand
                    # exactly as they are; this is the destructive
                    # scenario the staleness protocol exists to prevent.
                    unchanged += 1
                    continue
                # Genuinely new content (a repo-direct edit), unrelated to
                # the live write: normal convergence wins. The live entries
                # may be retirement survivors (see _retire_or_delete), so
                # store the new content FIRST to have a survivor id before
                # touching them. (An empty new file has no section to
                # survive into -- falls back to a direct delete_fn sweep,
                # same as the ordinary changed-seed-file path below.)
                ids = _store_sections(filename, path)
                if ids:
                    _retire_or_delete(prior["entry_ids"], ids[0])
                else:
                    for entry_id in prior["entry_ids"]:
                        delete_fn(techne_db, entry_id)
                _write_seed_row(filename, digest, ids)
                seeded += len(ids)
                continue

            if prior["sha256"] == digest:
                unchanged += 1
                continue

            for entry_id in prior["entry_ids"]:
                delete_fn(techne_db, entry_id)
            seeded += _reseed(filename, path, digest)

        return {"seeded": seeded, "removed": removed, "unchanged": unchanged}
    finally:
        conn.close()


def ingest_intake(
    intake_dir: Path,
    techne_db: Path,
    state_db: Path,
    store_fn,
    delete_fn,
    list_ids_fn: Callable[[Path, str], list[int]] | None = None,
) -> dict:
    """One-shot append pass over bundle/intake/*.{md,txt}.

    Opposite lifecycle from sync(): ledgered by content-sha (not filename),
    so filename reuse with new content is a new ingestion, and a file's
    disappearance from disk removes nothing — intake files are a disposable
    delivery vehicle, the memories they produce are not. There is never a
    removal pass here; a missing intake_dir simply yields no files (same
    Path.glob-on-nonexistent-dir behavior sync() already relies on).

    Each newly-ledgered file is stored under a source scoped to its own
    content-sha (`intake:<filename>#<sha12>`), not just its filename. That
    scoping is what lets a crash between storing entries and writing the
    ledger row converge without duplicates: on the next boot, list_ids_fn
    (when provided) is asked for that exact sha-scoped source, so it can
    only ever find that same orphaned run's rows — never a legitimately
    reused filename's entries, which get their own sha and their own source.
    """
    conn = _state_conn(state_db)
    try:
        ledgered = {row[0] for row in conn.execute("SELECT sha256 FROM intake_state")}
        files = sorted(intake_dir.glob("*.md")) + sorted(intake_dir.glob("*.txt"))
        ingested = skipped = 0

        for path in files:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in ledgered:
                skipped += 1
                continue

            source = f"intake:{path.name}#{digest[:12]}"
            if list_ids_fn is not None:
                for entry_id in list_ids_fn(techne_db, source):
                    delete_fn(techne_db, entry_id)

            ids: list[int] = []
            try:
                for section in parse_knowledge_file(path):
                    ids.append(
                        store_fn(techne_db, section, type_for(path.name), "intake", source)
                    )
            except Exception:
                # Best-effort cleanup so a mid-file store failure never
                # orphans entries the state db has no record of.
                for entry_id in ids:
                    try:
                        delete_fn(techne_db, entry_id)
                    except Exception:
                        log.warning(
                            "cleanup failed for orphaned entry %s from %s", entry_id, path.name
                        )
                raise

            conn.execute(
                "INSERT OR REPLACE INTO intake_state "
                "(sha256, filename, entry_ids) VALUES (?, ?, ?)",
                (digest, path.name, json.dumps(ids)),
            )
            conn.commit()
            ledgered.add(digest)
            ingested += len(ids)

        return {"ingested": ingested, "skipped": skipped}
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="seeder: %(message)s")
    if len(argv) != 2:
        log.error("usage: seeder.py <bundle_dir>")
        return 2
    bundle = Path(argv[1])
    knowledge_dir = bundle / "knowledge"
    # knowledge_dir may legitimately not exist: git drops empty directories,
    # so deleting the last knowledge file from a bundle removes knowledge/
    # from the built image entirely, not merely empties it. sync() already
    # treats a missing knowledge_dir as zero current files (Path.glob on a
    # nonexistent dir yields nothing, same as an empty one), so it must
    # still run here — otherwise state-tracked removals never purge and
    # the deleted file's seeded rows persist in techne forever.
    home = Path(os.environ.get("TECHNE_HOME", "/techne"))
    home.mkdir(parents=True, exist_ok=True)
    techne_db = home / "techne.db"

    from techne.core import knowledge
    from techne.core.database import init_db

    init_db(techne_db).close()

    def store_fn(db_path, content, type, tags, source):
        return knowledge.store(
            db_path, content, type=type, tags=tags, confidence="high", source=source
        )

    def _seed_sourced_entries(db_path):
        # No source filter on list_entries — enumerate everything (a large
        # limit stands in for "unbounded"; seeded corpora are small local
        # knowledge files) and narrow to seed-sourced rows here.
        # include_superseded=True so superseded rows (excluded by default)
        # are included too — otherwise stale seed-sourced rows would
        # survive a purge or a ground-truth convergence sweep instead of
        # being cleared with everything else.
        return [
            entry
            for entry in knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True)
            if (entry.get("source") or "").startswith("seed:")
        ]

    def _ids_by_source(db_path, source):
        # Reuses the purge helper's enumeration above, narrowed to an exact
        # source match — wired in as sync()'s list_ids_fn so the
        # ground-truth convergence path can find entries a SIGKILL left
        # behind between a store_fn() call and the state-row commit.
        return [
            entry["id"] for entry in _seed_sourced_entries(db_path) if entry.get("source") == source
        ]

    def purge_fn(db_path):
        for entry in _seed_sourced_entries(db_path):
            knowledge.delete(db_path, entry["id"])

    def retire_fn(db_path, entry_ids, survivor_id):
        # A lane='live' row's entries are very often themselves retirement
        # survivors from the fast lane's own earlier edit (see
        # src/daedalus/memory/teach.py's apply_knowledge, which always
        # retires a file's prior seed/intake entries INTO its freshly
        # stored entry) -- hard-deleting one here would trip techne's own
        # foreign key on knowledge.superseded_by. Retiring into the new
        # survivor instead is always safe and mirrors exactly what the
        # fast lane does for its own edits. Mirrors
        # daedalus.memory.teach._retire_entries's own skip-if-already-gone
        # guard (duplicated, not imported: sibling deployables, not a
        # shared dependency -- see docker/seeder.py's module docstring).
        from techne.core.database import connect
        from techne.core.knowledge import _retire_in_conn

        conn = connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for entry_id in entry_ids:
                if entry_id == survivor_id:
                    continue
                row = conn.execute(
                    "SELECT superseded_by FROM knowledge WHERE id = ?", (entry_id,)
                ).fetchone()
                if row is None or row["superseded_by"] is not None:
                    continue  # already gone, or already retired -- idempotent
                _retire_in_conn(conn, entry_id, survivor_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ids_by_intake_source(db_path, source):
        # Mirrors _ids_by_source above, wired in as ingest_intake()'s
        # list_ids_fn instead of sync()'s. An exact match against a
        # sha-scoped "intake:<filename>#<sha12>" source can never collide
        # with a "seed:"-sourced knowledge entry, so no prefix filter is
        # needed here the way _seed_sourced_entries needs one (that helper
        # is shared with purge_fn, which does need the broader sweep).
        return [
            entry["id"]
            for entry in knowledge.list_entries(db_path, limit=10_000_000, include_superseded=True)
            if entry.get("source") == source
        ]

    result = sync(
        knowledge_dir,
        techne_db,
        home / "seed_state.db",
        store_fn,
        knowledge.delete,
        purge_fn=purge_fn,
        list_ids_fn=_ids_by_source,
        retire_fn=retire_fn,
        skills_dir=bundle / "skills",
    )
    if not knowledge_dir.is_dir() and result == {"seeded": 0, "removed": 0, "unchanged": 0}:
        # Truly never-seeded bundle: no knowledge/ now, and no prior state to
        # purge either — a clean no-op, so keep the quieter fast-path log.
        log.info("no knowledge/ in bundle — nothing to seed")
    else:
        log.info("seeded=%(seeded)s removed=%(removed)s unchanged=%(unchanged)s", result)

    intake_dir = bundle / "intake"
    if intake_dir.is_dir():
        intake_result = ingest_intake(
            intake_dir,
            techne_db,
            home / "seed_state.db",
            store_fn,
            knowledge.delete,
            list_ids_fn=_ids_by_intake_source,
        )
        log.info("intake: ingested=%(ingested)s skipped=%(skipped)s", intake_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
