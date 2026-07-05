"""Per-task token budgets. Exhaustion is reason code 702 — the checkpoint
survives, so a caller can re-fund and resume."""

from __future__ import annotations


class TokenBudget:
    def __init__(self, total: int):
        self.total = total
        self.used = 0

    def spend(self, tokens: int) -> None:
        self.used += tokens

    def remaining(self) -> int:
        return max(0, self.total - self.used)
