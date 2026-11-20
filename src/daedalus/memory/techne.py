"""techne-backed personal memory behind the MemoryStore seam.

techne (github.com/tenishai/techne) installs from the tenish index as
tenish-techne. Recall and publish failures degrade to no memories and no
publish — a broken memory backend must never take the agent down.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# techne.api.recall only slices its result to `limit`; this keeps all of it.
_ALL_ROWS = 1000


class TechneMemoryStore:
    """recall() = this agent's live knowledge (techne search, soft-archived rows left
    out, #233), then the rows techne.api.recall finds in other stores: installed recall
    sources and federated stores, so a fact another agent published comes back through
    the team pool (#228). commit() =
    techne.knowledge.store; publish() = techne.api.publish, called once at task end,
    which sends knowledge carrying one of techne's `archivist_publish_tags` and does
    nothing when that list is empty. The agent never knows techne exists.

    techne's config comes from `home` when given (the bundle's memory.home), else
    from TECHNE_HOME, as for any techne process. `use_vectors` applies to this
    agent's own knowledge search, as before #228.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        use_vectors: bool = True,
        *,
        home: str | Path | None = None,
        recall_sources: list[str] | None = None,
    ):
        self.use_vectors = use_vectors
        self.home = Path(home).expanduser() if home else None
        # The bundle's memory.recall_sources (#265): installed `techne.recall_sources`
        # entry points this agent may consult, by name. Added to techne's
        # recall.allowed_sources in memory on every config load; the memory volume
        # is never written for it.
        self.recall_sources = [str(name) for name in (recall_sources or [])]
        if db_path is None:
            from techne.config import techne_home

            db_path = (self.home or techne_home()) / "techne.db"
        self.db_path = Path(db_path)

    def _config(self):
        from techne.config import Config

        cfg = Config.load(self.home / "config.yaml" if self.home else None)
        cfg.db_path = self.db_path
        if self.recall_sources:
            allowed = list(cfg.recall.allowed_sources)
            cfg.recall.allowed_sources = allowed + [
                name for name in self.recall_sources if name not in allowed
            ]
        return cfg

    async def recall_hits(self, query: str, k: int = 5) -> list[dict]:
        """Up to k of this agent's memories, then up to k hits from other stores, as
        {"content", "source"[, "ts", "source_url"]}: "local" for this agent's knowledge,
        else the store's or recall source's name (e.g. "liminal_archivist" for the team
        pool, "daffyne" for a transcript diary); ts and source_url when the row has them."""
        if not query.strip():
            return []
        try:
            return await asyncio.to_thread(self._recall_hits, query, k)
        except Exception:
            log.warning("techne recall failed; continuing without memories", exc_info=True)
            return []

    def _recall_hits(self, query: str, k: int) -> list[dict]:
        return self._local_hits(query, k) + self._remote_hits(query, k)

    def _local_hits(self, query: str, k: int) -> list[dict]:
        """This agent's knowledge, through techne search as before #228, but without
        soft-archived rows, as techne_recall leaves them out (#233). techne_recall's
        local page mixes in observations and threads, which could fill it and push
        every memory out (Prudence, PR 230)."""
        from techne.core import knowledge, search

        hits: list[dict] = []
        rows = search.search(
            self.db_path,
            query,
            table="knowledge",
            limit=k,
            use_vectors=self.use_vectors,
            include_archived=False,
        )
        for row in rows:
            content = row.get("content")
            if not content:
                continue
            if row.get("id") is not None:
                # search() clips a row to 500 characters, unmarked; a whole-file skill
                # procedure read through that clip is a summary posing as the
                # procedure. Re-fetch the full row; any failure keeps the clip.
                try:
                    full = knowledge.get(self.db_path, row["id"])
                except Exception:
                    full = None
                if full and full.get("content"):
                    content = full["content"]
            hits.append({"content": content, "source": "local"})
        return hits

    def _remote_hits(self, query: str, k: int) -> list[dict]:
        """Rows techne recall found outside this store: a recall source (named by
        `recall_source`) or a federated store (named by `store`). Local rows are
        skipped here; _local_hits has them."""
        from techne import api

        hits: list[dict] = []
        for row in api.recall(query, limit=_ALL_ROWS, config=self._config()):
            source = row.get("recall_source") or row.get("store", "local")
            if source == "local" or not row.get("content"):
                continue
            hit = {"content": row["content"], "source": source}
            # Provenance a diary row carries (#265): when it was said and where.
            # Kept only when present, so a pool row stays {content, source}.
            for key in ("ts", "source_url"):
                if row.get(key):
                    hit[key] = str(row[key])
            hits.append(hit)
            if len(hits) >= k:
                break
        return hits

    async def recall(self, query: str, k: int = 5) -> list[str]:
        return [h["content"] for h in await self.recall_hits(query, k)]

    async def commit(self, content: str, kind: str = "fact", tags: str = "") -> None:
        from techne.core import knowledge

        await asyncio.to_thread(knowledge.store, self.db_path, content, type=kind, tags=tags)

    async def publish(self) -> dict:
        """Send tagged knowledge to the team pool. Never raises; techne.api.publish
        reports a failed step in the result's "error"."""
        try:
            from techne import api

            # _config() reads config.yaml, so it runs in the thread too.
            return await asyncio.to_thread(lambda: api.publish(config=self._config()))
        except Exception as exc:
            log.warning("techne publish failed", exc_info=True)
            return {"enqueued": 0, "synced": 0, "failed": 0, "error": str(exc)}
