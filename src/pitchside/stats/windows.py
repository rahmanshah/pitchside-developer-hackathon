"""Time windows over the match clock.

Two primitives, both incremental (O(1) amortised per update) so the engine can
run live:

* ``RollingWindow``: sum and count of timestamped samples in the last
  ``span_s`` seconds, e.g. shots or xG in the last five minutes.
* ``PossessionClock``: who had the ball for how long, over any recent window
  or since kick-off.

Windows cover ``(now - span_s, now]``. Early in the match a window is shorter
than its span; ``covered_s`` says by how much, so rates are not understated.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class RollingWindow:
    __slots__ = ("span_s", "_samples", "_sum", "_last_clock")

    def __init__(self, span_s: float) -> None:
        if span_s <= 0:
            raise ValueError("span_s must be positive")
        self.span_s = float(span_s)
        self._samples: deque[tuple[float, float]] = deque()
        self._sum = 0.0
        self._last_clock = 0.0

    def add(self, clock_s: float, value: float = 1.0) -> None:
        if clock_s < self._last_clock:
            raise ValueError(f"clock went backwards: {clock_s} < {self._last_clock}")
        self.advance(clock_s)
        self._samples.append((clock_s, value))
        self._sum += value

    def advance(self, clock_s: float) -> None:
        """Move the window's end to `clock_s`, dropping samples that fell out."""
        self._last_clock = max(self._last_clock, clock_s)
        cutoff = self._last_clock - self.span_s
        samples = self._samples
        while samples and samples[0][0] <= cutoff:
            self._sum -= samples.popleft()[1]
        if not samples:
            self._sum = 0.0  # reset accumulated float error

    @property
    def sum(self) -> float:
        return self._sum

    @property
    def count(self) -> int:
        return len(self._samples)

    @property
    def mean(self) -> Optional[float]:
        return self._sum / len(self._samples) if self._samples else None

    def covered_s(self, start_s: float = 0.0) -> float:
        """Length of the window actually covered, given the clock started at `start_s`."""
        return max(min(self.span_s, self._last_clock - start_s), 0.0)


class PossessionClock:
    """Tracks possession spells and answers 'how long did team X have the ball'."""

    def __init__(self, keep_s: float = 1200.0) -> None:
        self.keep_s = keep_s
        self._spells: deque[tuple[float, float, str]] = deque()  # closed spells (start, end, team)
        self._team: Optional[str] = None
        self._since = 0.0
        self._totals: dict[str, float] = {}

    @property
    def team(self) -> Optional[str]:
        return self._team

    @property
    def since_s(self) -> float:
        """Clock at which the current possession spell started."""
        return self._since

    def switch(self, team: str, clock_s: float) -> None:
        if self._team is not None and clock_s > self._since:
            self._spells.append((self._since, clock_s, self._team))
            self._totals[self._team] = self._totals.get(self._team, 0.0) + clock_s - self._since
        self._team, self._since = team, clock_s
        cutoff = clock_s - self.keep_s
        while self._spells and self._spells[0][1] <= cutoff:
            self._spells.popleft()

    def seconds(self, team: str, start_s: float, end_s: float) -> float:
        """Seconds `team` had the ball within [start_s, end_s] (start_s within `keep_s`)."""
        total = 0.0
        for s, e, t in self._spells:
            if t == team:
                total += max(min(e, end_s) - max(s, start_s), 0.0)
        if self._team == team:
            total += max(end_s - max(self._since, start_s), 0.0)
        return total

    def total(self, team: str, clock_s: float) -> float:
        """Seconds `team` has had the ball since kick-off."""
        extra = clock_s - self._since if self._team == team else 0.0
        return self._totals.get(team, 0.0) + max(extra, 0.0)
