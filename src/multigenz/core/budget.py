"""Spend controls where usage is measurable. CLI/subscription spend is reported as unknown."""

from __future__ import annotations

from multigenz.errors import MultiGenzError
from multigenz.storage.sqlite import Store


class BudgetExceeded(MultiGenzError):
    pass


class Budget:
    def __init__(self, limit_usd: float | None, store: Store | None = None, run_id: str | None = None):
        self.limit = limit_usd
        self.store = store
        self.run_id = run_id
        self._spent = 0.0
        self.unknown = False

    @property
    def spent(self) -> float:
        if self.store is not None and self.run_id:
            row = self.store.get_run(self.run_id)
            return float(row["spent_usd"] or 0) if row else self._spent
        return self._spent

    def record(self, usd: float | None) -> None:
        if usd is None:
            self.unknown = True
            if self.store and self.run_id:
                self.store.update_run(self.run_id, cost_unknown=1)
            return
        if self.store is not None and self.run_id:
            self.store.update_run(self.run_id, spent_usd=self.spent + usd)
        else:
            self._spent += usd

    def check(self, what: str) -> None:
        """Raise before a *measurable* (API) call if the budget is already used up."""
        if self.limit is not None and self.spent >= self.limit:
            raise BudgetExceeded(f"run budget ${self.limit:.2f} exhausted (measured spend ${self.spent:.4f}); "
                                 f"refusing {what}")
