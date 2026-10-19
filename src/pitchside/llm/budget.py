"""Spending caps, enforced from the usage log before every model call.

Three caps: dollars per match, calls per match, and dollars across the whole usage log. Each call first
*reserves* its worst-case cost (estimated input plus the maximum output tokens); the reservation is settled
with the measured cost afterwards. Reservations count against the caps, so concurrent calls cannot overshoot.

The ledger is loaded from the log at startup, so restarting a run does not reset spending. It is per process:
two processes writing the same log do not see each other's calls after startup.
"""

from __future__ import annotations

import itertools
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from .errors import BudgetExhausted
from .usage import UsageLog

NO_MATCH = "(no match)"


@dataclass(frozen=True)
class Reservation:
    id: int
    match: str
    amount_usd: float


class BudgetGuard:
    def __init__(
        self,
        log: UsageLog,
        *,
        match_budget_usd: float,
        match_max_calls: int,
        total_budget_usd: float,
    ) -> None:
        self.match_budget_usd = match_budget_usd
        self.match_max_calls = match_max_calls
        self.total_budget_usd = total_budget_usd
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._spent: defaultdict[str, float] = defaultdict(float)
        self._calls: defaultdict[str, int] = defaultdict(int)
        self._total = 0.0
        self._pending: dict[int, Reservation] = {}
        for r in log.read():
            if r.status == "refused":
                continue  # never sent, never billed
            key = r.match_id or NO_MATCH
            self._spent[key] += r.cost_usd
            self._calls[key] += 1
            self._total += r.cost_usd

    def spent(self, match_id: Optional[str] = None) -> float:
        with self._lock:
            return self._spent[match_id or NO_MATCH]

    def total_spent(self) -> float:
        with self._lock:
            return self._total

    def calls(self, match_id: Optional[str] = None) -> int:
        with self._lock:
            return self._calls[match_id or NO_MATCH]

    def reserve(self, match_id: Optional[str], worst_case_usd: float) -> Reservation:
        """Hold ``worst_case_usd`` for one call, or raise ``BudgetExhausted`` without sending anything."""
        key = match_id or NO_MATCH
        with self._lock:
            pending_match = sum(p.amount_usd for p in self._pending.values() if p.match == key)
            pending_calls = sum(1 for p in self._pending.values() if p.match == key)
            pending_total = sum(p.amount_usd for p in self._pending.values())
            if self._total + pending_total + worst_case_usd > self.total_budget_usd:
                raise BudgetExhausted(
                    f"total model budget reached: ${self._total:.4f} spent of ${self.total_budget_usd:.2f} "
                    "(PITCHSIDE_TOTAL_BUDGET_USD)", scope="total",
                )
            if self._calls[key] + pending_calls + 1 > self.match_max_calls:
                raise BudgetExhausted(
                    f"match {key}: call cap reached ({self._calls[key]} of {self.match_max_calls}, "
                    "PITCHSIDE_MATCH_MAX_CALLS)", scope="match_calls",
                )
            if self._spent[key] + pending_match + worst_case_usd > self.match_budget_usd:
                raise BudgetExhausted(
                    f"match {key}: budget reached: ${self._spent[key]:.4f} spent of ${self.match_budget_usd:.2f} "
                    "(PITCHSIDE_MATCH_BUDGET_USD)", scope="match",
                )
            reservation = Reservation(next(self._ids), key, worst_case_usd)
            self._pending[reservation.id] = reservation
            return reservation

    def settle(self, reservation: Reservation, cost_usd: float) -> None:
        """Replace the hold with the measured cost of a call that was sent (0.0 if it failed unbilled)."""
        with self._lock:
            if self._pending.pop(reservation.id, None) is None:
                raise ValueError(f"reservation {reservation.id} already settled")
            self._spent[reservation.match] += cost_usd
            self._calls[reservation.match] += 1
            self._total += cost_usd
