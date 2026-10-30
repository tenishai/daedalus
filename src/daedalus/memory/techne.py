"""techne-backed personal memory behind the MemoryStore seam.

techne (github.com/tenishai/techne) installs from the tenish index as
tenish-techne. Recall and publish failures degrade to no memories and no
publish — a broken memory backend must never take the agent down.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from pathlib import Path

log = logging.getLogger(__name__)

# techne.api.recall only slices its result to `limit`; this keeps all of it.
_ALL_ROWS = 1000


class TechneMemoryStore:
    """recall() = techne.api.recall, the tiered recall techne_recall runs: this
    agent's knowledge plus every installed recall source, so a fact another agent
    published comes back through the team pool (#228). commit() =
    techne.knowledge.store; publish() = techne.api.publish, called once at task end,
    which sends knowledge carrying one of techne's `archivist_publish_tags` and does
    nothing when that list is empty. The agent never knows techne exists.

    techne's config comes from `home` when given (the bundle's memory.home), else
    from TECHNE_HOME, as for any techne process. `use_vectors` is no longer read:
    recall follows techne's own search settings, which fall back to FTS when no
    embedding backend is installed.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        use_vectors: bool | None = None,
        *,
        home: str | Path | None = None,
    ):
        if use_vectors is not None:
            warnings.warn(
                "TechneMemoryStore(use_vectors=...) is ignored since #228: recall "
                "follows techne's own search settings",
                DeprecationWarning,
                stacklevel=2,
            )
        self.home = Path(home).expanduser() if home else None
        if db_path is None:
            from techne.config import techne_home

            db_path = (self.home or techne_home()) / "techne.db"
        self.db_path = Path(db_path)

    def _config(self):
        from techne.config import Config

        cfg = Config.load(self.home / "config.yaml" if self.home else None)
        cfg.db_path = self.db_path
        return cfg

    async def recall_hits(self, query: str, k: int = 5) -> list[dict]:
        """Up to k hits as {"content", "source"}: "local" for this agent's own
        knowledge, else the recall source's name (e.g. "liminal_archivist" for the
        team pool). techne 0.19.1 searches at most 10 local rows, so k above 10 can
        add only recall-source rows."""
        if not query.strip():
            return []
        try:
            return await asyncio.to_thread(self._recall_hits, query, k)
        except Exception:
            log.warning("techne recall failed; continuing without memories", exc_info=True)
            return []

    def _recall_hits(self, query: str, k: int) -> list[dict]:
        from techne import api
        from techne.core import knowledge

        hits: list[dict] = []
        # Take every row techne returns (10 local, plus each recall source's own
        # page), in techne's order, then filter: observations and threads are
        # dropped below, so asking techne for exactly k could leave fewer than k
        # memories, and a fixed 10 could cut off the team pool's rows.
        for row in api.recall(query, limit=_ALL_ROWS, config=self._config()):
            source = row.get("recall_source")
            if source is None and row.get("source_table") != "knowledge":
                continue  # observations, threads, library documents: not memories
            content = row.get("content")
            if not content:
                continue
            if source is None and row.get("id") is not None:
                # Recall clips a local row to 500 characters, unmarked; a whole-file
                # skill procedure read through that clip is a summary posing as the
                # procedure. Re-fetch the full row; any failure keeps the clip.
                try:
                    full = knowledge.get(self.db_path, row["id"])
                except Exception:
                    full = None
                if full and full.get("content"):
                    content = full["content"]
            hits.append({"content": content, "source": source or "local"})
            if len(hits) >= k:
                break
        return hits

    async def recall(self, query: str, k: int = 5) -> list[str]:
        return [h["content"] for h in await self.recall_hits(query, k)]

    async def commit(self, content: str, kind: str = "fact", tags: str = "") -> None:
        from techne.core import knowledge

        knowledge.store(self.db_path, content, type=kind, tags=tags)

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
