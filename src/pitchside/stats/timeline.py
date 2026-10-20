"""A recorded match, replayed once through the stats engine, queryable "as of" any moment.

This is the data layer behind the MCP server. The no-future-leakage rule lives here, in one place: the only way to
read anything is ``MatchTimeline.as_of(as_of_s)``, which returns a ``TimelineView`` holding the stats snapshot,
the findings and the clock **as they were at that moment**. Nothing in a view is computed from, or can reach, an
event after the bound.

The bound is ``as_of_s`` (match seconds, inclusive). Some events share a clock reading, so callers that need an
exact position (the agents freeze a finding's snapshot at the event that triggered it) can add ``as_of_event_id``,
which can only make the view *tighter*: events with a larger id are excluded as well.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from ..schema import Event, MatchStartEvent, Score
from .engine import Snapshot, StatsEngine
from .findings import Finding


class TimelineError(ValueError):
    """A request the timeline cannot answer (bad bound, unknown match). The message never reveals future data."""


@dataclass(frozen=True)
class TimelineView:
    """Everything known at one moment of one match."""

    match_id: str
    as_of_s: float
    data_through_s: float  # clock of the last event included; at most as_of_s
    snapshot: Snapshot
    findings: tuple[Finding, ...]  # detected by this moment, in detection order

    @property
    def score(self) -> Score:
        return self.snapshot.score

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> "TimelineView":
        """A view over a snapshot frozen earlier (the interpreter's local tools). It carries no findings."""
        return cls(snapshot.match_id, snapshot.clock_s, snapshot.clock_s, snapshot, ())


def check_bound(as_of_s: float, as_of_event_id: Optional[int] = None) -> None:
    if isinstance(as_of_s, bool) or not isinstance(as_of_s, (int, float)) or not math.isfinite(as_of_s) or as_of_s < 0:
        raise TimelineError("as_of_s must be a finite number of match seconds, 0 or more")
    if as_of_event_id is not None and (isinstance(as_of_event_id, bool) or not isinstance(as_of_event_id, int)
                                       or as_of_event_id < 0):
        raise TimelineError("as_of_event_id must be an event id, 0 or more")


class MatchTimeline:
    def __init__(self, events: Iterable[Event]) -> None:
        self._events = list(events)
        if not self._events or not isinstance(self._events[0], MatchStartEvent):
            raise TimelineError("a match must start with a match_start event")
        start = self._events[0]
        self.match_id: str = start.match_id
        self.home_id, self.away_id = start.home.team_id, start.away.team_id
        self.team_names = {start.home.team_id: start.home.name, start.away.team_id: start.away.name}
        self._clocks: list[float] = []
        self._event_ids: list[int] = []
        self._snapshots: list[Snapshot] = []
        self._findings: list[Finding] = []
        self._finding_count_after: list[int] = []  # findings detected up to and including event i
        engine = StatsEngine()
        for event in self._events:
            if event.match_id != self.match_id:
                raise TimelineError("all events must belong to one match")
            if self._clocks and (event.clock_s < self._clocks[-1] or event.event_id <= self._event_ids[-1]):
                raise TimelineError("events must be in order")
            self._findings.extend(engine.ingest(event))
            self._clocks.append(event.clock_s)
            self._event_ids.append(event.event_id)
            self._snapshots.append(engine.snapshot())
            self._finding_count_after.append(len(self._findings))

    def __len__(self) -> int:
        return len(self._events)

    def cut(self, as_of_s: float, as_of_event_id: Optional[int] = None) -> int:
        """How many events are inside the bound (events 0 .. cut-1)."""
        check_bound(as_of_s, as_of_event_id)
        count = bisect_right(self._clocks, as_of_s)
        if as_of_event_id is not None:
            count = min(count, bisect_right(self._event_ids, as_of_event_id))
        return count

    def as_of(self, as_of_s: float, as_of_event_id: Optional[int] = None) -> TimelineView:
        count = self.cut(as_of_s, as_of_event_id)
        if count == 0:
            raise TimelineError("the match has no events at or before that moment")
        last = count - 1
        return TimelineView(
            match_id=self.match_id, as_of_s=float(as_of_s), data_through_s=self._clocks[last],
            snapshot=self._snapshots[last], findings=tuple(self._findings[:self._finding_count_after[last]]),
        )


class MatchStore:
    """The matches a server can answer about. Read-only for clients: only the host process adds matches."""

    def __init__(self) -> None:
        self._timelines: dict[str, MatchTimeline] = {}

    def add(self, events: Sequence[Event]) -> MatchTimeline:
        timeline = MatchTimeline(events)
        self._timelines[timeline.match_id] = timeline
        return timeline

    def __contains__(self, match_id: str) -> bool:
        return match_id in self._timelines

    def ids(self) -> list[str]:
        return sorted(self._timelines)

    def timelines(self) -> list[MatchTimeline]:
        return [self._timelines[i] for i in self.ids()]

    def get(self, match_id: Optional[str] = None) -> MatchTimeline:
        """The named match, or the only one when there is exactly one and no id is given."""
        if match_id is None:
            if len(self._timelines) == 1:
                return next(iter(self._timelines.values()))
            raise TimelineError("match_id is required: this server holds " + (
                f"{len(self._timelines)} matches (list_matches shows them)" if self._timelines else "no match yet"))
        try:
            return self._timelines[match_id]
        except KeyError:
            raise TimelineError(f"unknown match_id {match_id!r} (list_matches shows the available matches)") from None
