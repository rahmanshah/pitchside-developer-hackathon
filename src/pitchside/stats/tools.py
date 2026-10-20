"""Stats tools: plain functions over a stats snapshot, returning context numbers in one shape.

The interpreter agent calls these as Agent Framework function tools today; milestone 4 serves the same functions
from an MCP server. Each returns ``{"items": [{metric, team_id, value, unit, window_start_s, window_end_s}]}`` so
the caller can give every number a ledger id before a model sees it.
"""

from __future__ import annotations

from typing import Any, Optional

from .engine import Snapshot


def _item(metric: str, team_id: Optional[str], value: Optional[float], unit: str, start: float,
          end: float) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    return {"metric": metric, "team_id": team_id, "value": round(float(value), 3), "unit": unit,
            "window_start_s": round(start, 1), "window_end_s": round(end, 1)}


def match_totals(snapshot: Snapshot) -> dict[str, Any]:
    """Since kick-off, per club: possession, shots, xG, pass completion."""
    items = []
    for team_id, t in snapshot.totals.items():
        items += [
            _item("possession_total", team_id, t.possession_pct, "pct", 0.0, snapshot.clock_s),
            _item("shots_total", team_id, t.shots, "count", 0.0, snapshot.clock_s),
            _item("xg_total", team_id, t.xg, "xg", 0.0, snapshot.clock_s),
            _item("pass_completion_total", team_id, t.pass_completion_pct, "pct", 0.0, snapshot.clock_s),
        ]
    return {"items": [i for i in items if i is not None]}


def recent_window(snapshot: Snapshot) -> dict[str, Any]:
    """The rolling window (last few minutes), per club: field tilt, possession, shots, xG, pressures per minute."""
    items = []
    for team_id, t in snapshot.teams.items():
        start, end = t.window_start_s, t.window_end_s
        items += [
            _item("field_tilt", team_id, t.field_tilt_pct, "pct", start, end),
            _item("possession", team_id, t.possession_pct, "pct", start, end),
            _item("shots", team_id, t.shots, "count", start, end),
            _item("xg", team_id, t.xg, "xg", start, end),
            _item("pressures_per_min", team_id, t.pressures_per_min, "per_min", start, end),
        ]
    return {"items": [i for i in items if i is not None]}


TOOLS = {"match_totals": match_totals, "recent_window": recent_window}
