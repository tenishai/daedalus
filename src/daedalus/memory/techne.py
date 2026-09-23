"""techne-backed personal memory behind the MemoryStore seam.

techne (github.com/tenishai/techne) is not on PyPI; install it alongside:
pip install -e path/to/techne. Recall failures degrade to no memories —
a broken memory backend must never take the agent down.
"""

from __future__ import annotations

from pathlib import Path


class TechneMemoryStore:
    """recall() = FTS/hybrid search over techne's knowledge table;
    commit() = techne.knowledge.store. The agent never knows techne exists.

    use_vectors defaults True (techne 0.16.1 wave): the runtime image bakes
    the embedding backend and model in, and techne's search degrades to
    FTS-only cleanly when a backend is genuinely absent (the cold-cache
    hang was fixed in techne 0.15.0, techne#708). Vectors-on requires
    techne >= 0.16.1 specifically — before techne#827 the vec-first fusion
    could drop every lexical hit, which is worse than no vectors at all.
    """

    def __init__(self, db_path: str | Path | None = None, use_vectors: bool = True):
        if db_path is None:
            from techne.config import techne_home

            db_path = techne_home() / "techne.db"
        self.db_path = Path(db_path)
        self.use_vectors = use_vectors

    async def recall(self, query: str, k: int = 5) -> list[str]:
        from techne.core import knowledge, search

        if not query.strip():
            return []
        try:
            rows = search.search(
                self.db_path, query, table="knowledge", limit=k, use_vectors=self.use_vectors
            )
        except Exception:
            return []
        results: list[str] = []
        for row in rows:
            content = row.get("content")
            if not content:
                continue
            # search() clips every row's content to 500 characters, unmarked
            # (techne search.py, both 0.14 and 0.16.1) -- a whole-file skill
            # procedure read through that clip is a summary posing as the
            # procedure. Re-fetch the full row by id; any failure keeps the
            # clipped copy rather than dropping the memory.
            if row.get("source_table") == "knowledge" and row.get("id") is not None:
                try:
                    full = knowledge.get(self.db_path, row["id"])
                except Exception:
                    full = None
                if full and full.get("content"):
                    content = full["content"]
            results.append(content)
        return results

    async def commit(self, content: str, kind: str = "fact") -> None:
        from techne.core import knowledge

        knowledge.store(self.db_path, content, type=kind)
