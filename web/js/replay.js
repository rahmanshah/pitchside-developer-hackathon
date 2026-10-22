// Replay engine: the replay timeline and a clock that plays, pauses, seeks and changes speed.
// Pure: no DOM, no Date.now(). Every call takes the wall time it needs, so it is deterministic and unit-tested
// (tests/test_web_js.py runs this file in V8). Times are replay-timeline seconds (docs/OVERLAY.md), not match clock.

export function clamp(x, lo, hi) {
  return Math.min(Math.max(x, lo), hi);
}

// timeline: overlay.json's `timeline` ({duration_s, segments: [{kind, period, start_s, end_s, clock_*}]}).
export function makeTimeline(timeline) {
  const segments = timeline.segments;
  const plays = segments.filter((s) => s.kind === "play");
  const duration = timeline.duration_s;

  function playSegment(clock, period) {
    if (period != null) {
      const seg = plays.find((s) => s.period === period);
      if (seg) return seg;
    }
    // A clock on a period boundary belongs to the earlier period (same rule as the Python builder).
    return plays.find((s) => clock <= s.clock_end_s) ?? plays[plays.length - 1];
  }

  return {
    duration,
    segments,
    // Match clock (+ period) -> replay timeline.
    toTimeline(clock, period) {
      const seg = playSegment(clock, period);
      return seg.start_s + clamp(clock, seg.clock_start_s, seg.clock_end_s) - seg.clock_start_s;
    },
    // Replay timeline -> {segment, phase, period, clock}.
    at(t) {
      const tt = clamp(t, 0, duration);
      let seg = segments.find((s) => s.start_s <= tt && tt < s.end_s);
      if (!seg) seg = segments[segments.length - 1];
      const clock = seg.kind === "play" ? seg.clock_start_s + (tt - seg.start_s) : seg.clock_end_s;
      return { segment: seg, phase: seg.kind, period: seg.period, clock };
    },
  };
}

// "72:14" while playing, "HT" in the break, "FT" after the final whistle.
export function clockLabel(at) {
  if (at.phase === "break") return "HT";
  if (at.phase === "end") return "FT";
  const whole = Math.floor(at.clock);
  const mm = Math.floor(whole / 60);
  const ss = whole % 60;
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

// The minute as broadcasters say it: 1st minute = 0:00-0:59 -> "1'".
export function minuteLabel(clock) {
  return `${Math.floor(clock / 60) + 1}'`;
}

// Parses ?t= values: "3800", "3800.5" (timeline seconds) or "63:20" (match clock, mapped with the timeline).
export function parseTime(text, timeline) {
  if (text == null || text === "") return null;
  const m = /^(\d+):(\d{1,2})(?:@(\d))?$/.exec(String(text).trim());
  if (m) {
    const clock = Number(m[1]) * 60 + Number(m[2]);
    const period = m[3] ? Number(m[3]) : clock > 2700 ? 2 : 1;
    return timeline.toTimeline(clock, period);
  }
  const n = Number(text);
  return Number.isFinite(n) ? clamp(n, 0, timeline.duration) : null;
}

// holds: [{at, ms}] - when playback faster than 1x crosses `at`, the clock holds there for `ms` of wall time,
// so a lower third can be read. Seeking never triggers a hold at the seek point itself.
export class ReplayClock {
  constructor({ duration, speed = 1, holds = [], holdsEnabled = true } = {}) {
    this.duration = duration;
    this.speed = speed;
    this.holds = [...holds].filter((h) => h.ms > 0).sort((a, b) => a.at - b.at);
    this.holdsEnabled = holdsEnabled;
    this.playing = false;
    this.anchorT = 0;
    this.anchorWall = 0;
  }

  _walk(now) {
    let t0 = this.anchorT;
    let w0 = this.anchorWall;
    if (this.holdsEnabled && this.speed > 1) {
      for (const h of this.holds) {
        if (h.at <= t0) continue;
        if (h.at > this.duration) break;
        const reach = w0 + ((h.at - t0) / this.speed) * 1000;
        if (now < reach) break;
        if (now < reach + h.ms) return { t: h.at, holding: true };
        t0 = h.at;
        w0 = reach + h.ms;
      }
    }
    return { t: Math.min(t0 + ((now - w0) / 1000) * this.speed, this.duration), holding: false };
  }

  time(now) {
    return this.playing ? this._walk(now).t : this.anchorT;
  }

  holding(now) {
    return this.playing && this._walk(now).holding;
  }

  ended(now) {
    return this.time(now) >= this.duration;
  }

  play(now) {
    const t = this.time(now);
    this.anchorT = t >= this.duration ? 0 : t;
    this.anchorWall = now;
    this.playing = true;
  }

  pause(now) {
    this.anchorT = this.time(now);
    this.playing = false;
  }

  toggle(now) {
    if (this.playing) this.pause(now);
    else this.play(now);
  }

  seek(t, now) {
    this.anchorT = clamp(t, 0, this.duration);
    this.anchorWall = now;
  }

  setSpeed(speed, now) {
    this.anchorT = this.time(now);
    this.anchorWall = now;
    this.speed = speed;
  }
}
