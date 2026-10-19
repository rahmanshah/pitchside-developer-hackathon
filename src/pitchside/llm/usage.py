"""Token and cost log: one JSON record per model-call attempt, appended to a JSONL file.

The log is the source of truth for spending. The budget guard reloads it at startup, so restarting a run
does not reset what a match (or the whole log) has spent.
"""

from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Status = Literal["ok", "error", "refused"]


class UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    match_id: Optional[str] = None
    finding_id: Optional[str] = None
    role: str
    deployment: str
    model: Optional[str] = Field(None, description="Model name the service reported, if any.")
    attempt: int = Field(1, ge=1)
    status: Status
    error_kind: Optional[str] = None
    detail: Optional[str] = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    price_model: str
    price_version: str


class UsageLog:
    """Append-only JSONL log. Safe to share between threads and tasks in one process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.bad_lines = 0

    def append(self, record: UsageRecord) -> None:
        line = record.model_dump_json() + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def read(self) -> Iterator[UsageRecord]:
        """All parseable records. Unparseable lines (e.g. a crash mid-write) are counted in ``bad_lines``."""
        self.bad_lines = 0
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    yield UsageRecord.model_validate_json(line)
                except ValidationError:
                    self.bad_lines += 1


@dataclass
class SummaryRow:
    key: str
    attempts: int = 0
    ok: int = 0
    errors: int = 0
    refused: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)

    def add(self, r: UsageRecord) -> None:
        self.attempts += 1
        self.ok += r.status == "ok"
        self.errors += r.status == "error"
        self.refused += r.status == "refused"
        self.input_tokens += r.input_tokens
        self.cached_tokens += r.cached_tokens
        self.output_tokens += r.output_tokens
        self.cost_usd += r.cost_usd
        if r.status != "refused":
            self.latencies.append(r.latency_ms)

    def percentile(self, q: float) -> Optional[float]:
        if not self.latencies:
            return None
        if len(self.latencies) == 1:
            return self.latencies[0]
        return statistics.quantiles(self.latencies, n=100, method="inclusive")[round(q) - 1]


GroupBy = Literal["role", "match", "model"]


def summarise(records: Iterable[UsageRecord], by: GroupBy = "role") -> tuple[list[SummaryRow], SummaryRow]:
    """Rows grouped by role, match or model (sorted by key), plus a total row."""
    rows: dict[str, SummaryRow] = {}
    total = SummaryRow("total")
    for r in records:
        key = {"role": r.role, "match": r.match_id or "-", "model": r.model or r.deployment}[by]
        rows.setdefault(key, SummaryRow(key)).add(r)
        total.add(r)
    return [rows[k] for k in sorted(rows)], total


def format_summary(rows: list[SummaryRow], total: SummaryRow, by: GroupBy) -> str:
    def ms(value: Optional[float]) -> str:
        return "-" if value is None else f"{value:.0f}"

    header = f"{by:<28} {'calls':>6} {'ok':>5} {'err':>5} {'refused':>7} {'input':>9} {'cached':>8} {'output':>8} {'cost $':>10} {'p50 ms':>7} {'p95 ms':>7}"
    lines = [header, "-" * len(header)]
    for row in [*rows, total] if rows else [total]:
        if row is total and rows:
            lines.append("-" * len(header))
        lines.append(
            f"{row.key[:28]:<28} {row.attempts:>6} {row.ok:>5} {row.errors:>5} {row.refused:>7} "
            f"{row.input_tokens:>9} {row.cached_tokens:>8} {row.output_tokens:>8} {row.cost_usd:>10.6f} "
            f"{ms(row.percentile(50)):>7} {ms(row.percentile(95)):>7}"
        )
    return "\n".join(lines)
