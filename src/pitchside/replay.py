"""Replay a recorded match as a live stream.

Events are released according to their match clock, scaled by ``speed``
(``speed=60`` plays a 90-minute match in 90 seconds). The sink is any callable,
so the same replay can feed stdout, an Azure Event Hub producer or a test list.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Optional

from .schema import Event


def replay(
    events: Iterable[Event],
    sink: Callable[[Event], None],
    speed: Optional[float] = 60.0,
    *,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Deliver events to `sink` in match-clock order. Returns the number delivered.

    `speed=None` delivers everything immediately. `sleep` and `now` are injectable
    so tests do not have to wait in real time.
    """
    if speed is not None and speed <= 0:
        raise ValueError("speed must be positive, or None for no delay")

    started = now()
    first_clock: Optional[float] = None
    delivered = 0
    for event in events:
        if first_clock is None:
            first_clock = event.clock_s
        if speed is not None:
            delay = (event.clock_s - first_clock) / speed - (now() - started)
            if delay > 0:
                sleep(delay)
        sink(event)
        delivered += 1
    return delivered
