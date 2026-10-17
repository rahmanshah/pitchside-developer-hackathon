"""Reading and writing matches as JSON Lines (one event per line)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Iterator

from .schema import Event, parse_event, to_json_line


def write_events(events: Iterable[Event], path: Path) -> int:
    """Write events to `path`, one JSON object per line. Returns the number written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(to_json_line(event) + "\n")
            count += 1
    return count


def read_events(path: Path) -> Iterator[Event]:
    """Yield validated events from a JSONL file; invalid lines raise with their line number."""
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield parse_event(line)
            except ValueError as error:  # pydantic.ValidationError is a ValueError
                raise ValueError(f"{path}:{number}: invalid event: {error}") from error


def write_answer_key(key: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(key, indent=2) + "\n", encoding="utf-8")
