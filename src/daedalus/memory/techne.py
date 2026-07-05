"""techne-backed personal memory behind the MemoryStore seam.

techne (github.com/dimartin/techne) is not on PyPI; install it alongside:
pip install -e path/to/techne. Recall failures degrade to no memories —
a broken memory backend must never take the agent down.
"""

from __future__ import annotations

from pathlib import Path


class TechneMemoryStore:
    """recall() = FTS/hybrid search over techne's knowledge table;
    commit() = techne.knowledge.store. The agent never knows techne exists."""

    def __init__(self, db_path: str | Path | None = None, use_vectors: bool = False):
        if db_path is None:
            from techne.config import techne_home

            db_path = techne_home() / "techne.db"
        self.db_path = Path(db_path)
        self.use_vectors = use_vectors

    async def recall(self, query: str, k: int = 5) -> list[str]:
        from techne.core import search

        if not query.strip():
            return []
        try:
            rows = search.search(
                self.db_path, query, table="knowledge", limit=k, use_vectors=self.use_vectors
            )
        except Exception:
            return []
        return [row["content"] for row in rows if row.get("content")]

    async def commit(self, content: str, kind: str = "fact") -> None:
        from techne.core import knowledge

        knowledge.store(self.db_path, content, type=kind)
