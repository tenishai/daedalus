"""The sqlite checkpoint store runs off the event loop (#205)."""

from __future__ import annotations

import asyncio
import time

from daedalus.checkpoints import SqliteCheckpoints


async def test_save_load_and_drop_do_not_block_the_event_loop(tmp_path, monkeypatch):
    store = SqliteCheckpoints(tmp_path / "cp.db")
    real_save = store._save

    def slow_save(task_id, state):
        time.sleep(0.3)
        real_save(task_id, state)

    monkeypatch.setattr(store, "_save", slow_save)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    task = asyncio.create_task(ticker())
    await store.save("t1", {"step": 1})
    task.cancel()
    assert ticks >= 5, "the loop was blocked during the save"
    assert await store.load("t1") == {"step": 1}
    await store.drop("t1")
    assert await store.load("t1") is None
