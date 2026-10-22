// What is on screen at replay time t: active overlays, score, stats snapshot, recent events, ball position.
// Pure: everything is derived from t by binary search over sorted arrays, never updated step by step, so seeking
// backwards gives exactly the same answer as playing up to the same point (tests/test_web_js.py).

// Last index i with times[i] <= t, or -1.
export function lastAtOrBefore(times, t) {
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= t) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return found;
}

// [{overlay, placement}] for one anchor, highest priority first, then most recent first.
export function activePlacements(overlays, t, anchor) {
  const out = [];
  for (const overlay of overlays) {
    for (const placement of overlay.placements) {
      if (placement.anchor === anchor && placement.show_at_s <= t && t < placement.hide_at_s) {
        out.push({ overlay, placement });
      }
    }
  }
  out.sort((a, b) => b.overlay.priority - a.overlay.priority || b.placement.show_at_s - a.placement.show_at_s);
  return out;
}

export function lowerThirdAt(overlays, t) {
  const active = activePlacements(overlays, t, "lower_third");
  return active.length ? active[0] : null;
}

// The overlay most recently started at or before t (what the persona comparison shows between moments).
export function latestStarted(overlays, t) {
  let best = null;
  for (const o of overlays) {
    if (o.show_at_s <= t && (!best || o.show_at_s >= best.show_at_s)) best = o;
  }
  return best;
}

// Where the ball was at an event (passes start where they are played).
export function eventPoint(e) {
  if (e.start) return e.start;
  if (e.location) return e.location;
  return null;
}

// events: parsed events.jsonl; timeline: makeTimeline(). Returns events on the replay timeline, sorted.
export function indexEvents(events, timeline) {
  const list = events
    .filter((e) => e.type !== "match_start")
    .map((e) => ({ ...e, t: timeline.toTimeline(e.clock_s, e.period) }))
    .sort((a, b) => a.t - b.t || a.event_id - b.event_id);
  const located = list.filter((e) => eventPoint(e));
  const goals = list.filter((e) => e.type === "shot" && e.outcome === "goal" && e.score_after);
  const byId = new Map(list.map((e) => [e.event_id, e]));
  return {
    list,
    times: list.map((e) => e.t),
    located,
    locatedTimes: located.map((e) => e.t),
    goals,
    goalTimes: goals.map((e) => e.t),
    byId,
  };
}

export function scoreAt(index, t) {
  const i = lastAtOrBefore(index.goalTimes, t);
  return i < 0 ? { home: 0, away: 0 } : { ...index.goals[i].score_after };
}

// Events with t0 < e.t <= t1, oldest first.
export function eventsBetween(index, t0, t1) {
  const hi = lastAtOrBefore(index.times, t1);
  const lo = lastAtOrBefore(index.times, t0);
  return index.list.slice(lo + 1, hi + 1);
}

// Ball position at t: linear between the locations of the events either side (an estimate from event locations;
// there is no tracking data).
export function ballAt(index, t) {
  const i = lastAtOrBefore(index.locatedTimes, t);
  if (i < 0) return { x: 52.5, y: 34, event: null };
  const a = index.located[i];
  const b = index.located[i + 1];
  const pa = eventPoint(a);
  if (!b || b.t <= a.t) return { x: pa.x, y: pa.y, event: a };
  const pb = eventPoint(b);
  const f = Math.min(Math.max((t - a.t) / (b.t - a.t), 0), 1);
  return { x: pa.x + (pb.x - pa.x) * f, y: pa.y + (pb.y - pa.y) * f, event: a };
}

// snapshots: parsed stats.jsonl; mapped onto the timeline once (a snapshot on the half-time boundary is the
// first half's last one).
export function indexSnapshots(snapshots, timeline) {
  const list = snapshots.map((s) => ({ ...s, t: timeline.toTimeline(s.clock_s) })).sort((a, b) => a.t - b.t);
  return { list, times: list.map((s) => s.t) };
}

export function snapshotAt(index, t) {
  const i = lastAtOrBefore(index.times, t);
  return i < 0 ? null : index.list[i];
}
