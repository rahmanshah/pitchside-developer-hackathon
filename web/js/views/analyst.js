// Analyst dashboard: 2D pitch with recent events, live stats strips, momentum and control-vs-chaos sparklines,
// the current moment in three personas side by side, model usage, and the key-moment timeline.
import { kindLabel, personaLabel, t as tr } from "../i18n.js";
import { minuteLabel } from "../replay.js";
import { eventsBetween } from "../timeline.js";
import { crest } from "./crest.js";
import { clear, fmt, h, s, setText } from "./dom.js";
import { highlightShape, pitchSvg } from "./pitch.js";
import { figuredText, provenanceChips, variantFor } from "./text.js";

const WINDOW_S = 120;
const TYPES = ["pass", "shot", "tackle", "pressure", "possession_change"];
const TYPE_LABEL = { pass: "passes", shot: "shots", tackle: "tackles", pressure: "pressuresShort", possession_change: "turnovers" };
const SPARK_W = 320;
const SPARK_H = 56;
const SPELL_ROW = 15; // key-moment timeline: height of one row of spells (px)
const SPELLS_TOP = 30; // must match .tl-lane.spells { top } in analyst.css

export function createAnalyst(root, ctx) {
  const { home, away, teams, tl } = ctx;
  const accent = (teamId) => teams[teamId]?.colours.accent ?? "var(--chalk-2)";
  const shown = new Set(TYPES);
  let windowMode = "window";

  // ---- head: compact scoreboard
  const scoreEl = h("span", { class: "an-score" }, "0 – 0");
  const clockEl = h("span", { class: "an-clock" }, "00:00");
  const head = h("header", { class: "an-head" },
    h("span", { class: "an-team" }, crest(home, { size: 28 }), h("span", {}, home.name)),
    scoreEl,
    h("span", { class: "an-team away" }, h("span", {}, away.name), crest(away, { size: 28 })),
    clockEl);

  // ---- pitch
  const pitchTitle = h("h2", { class: "card-title" });
  const filters = h("div", { class: "filters", role: "group", "aria-label": "Event types" });
  const pitch = pitchSvg({ cls: "pitch-svg an-pitch-svg", label: "Pitch: recent events" });
  const gHighlight = s("g", { class: "highlights" });
  const gEvents = s("g", { class: "events" });
  const gFocus = s("g", { class: "focus" });
  const ballDot = s("circle", { class: "an-ball", r: 0.9 });
  pitch.append(gHighlight, gEvents, gFocus, ballDot);
  const legend = h("p", { class: "legend" });
  const pitchCard = h("section", { class: "card an-pitch" },
    h("div", { class: "card-head" }, pitchTitle, filters), pitch, legend);

  // ---- stats
  const statsTitle = h("h2", { class: "card-title" });
  const windowSeg = h("div", { class: "seg small", role: "group", "aria-label": "Stats window" });
  const strips = h("div", { class: "strips" });
  const sparks = h("div", { class: "sparks" });
  const statsCard = h("section", { class: "card an-stats" }, h("div", { class: "card-head" }, statsTitle, windowSeg), strips, sparks);

  // ---- usage
  const usageCard = h("section", { class: "card an-usage" });

  // ---- compare
  const compareCard = h("section", { class: "card an-compare", "aria-live": "off" });

  // ---- timeline
  const timelineCard = h("section", { class: "card an-timeline" });

  clear(root).append(h("div", { class: "an" }, head, pitchCard, statsCard, usageCard, compareCard, timelineCard));

  // ---------------------------------------------------------------- stats strips + sparklines
  const stripRows = {};
  function strip(key, digits, unit) {
    const hv = h("span", { class: "sv home" });
    const av = h("span", { class: "sv away" });
    const hb = h("i", { class: "sb-h", style: { background: home.colours.accent } });
    const ab = h("i", { class: "sb-a", style: { background: away.colours.accent } });
    const label = h("span", { class: "sl" });
    const row = h("div", { class: "strip", role: "group" }, label, hv, h("span", { class: "sbar", "aria-hidden": "true" }, hb, ab), av);
    stripRows[key] = { row, hv, av, hb, ab, label, digits, unit };
    return row;
  }
  strips.append(strip("possession_pct", 0, "%"), strip("field_tilt_pct", 0, "%"), strip("pressures_per_min", 1, ""));

  function sparkline(key, values, colourUp, colourDown) {
    const pts = ctx.sn.list.map((snap, i) => [(snap.t / tl.duration) * SPARK_W, SPARK_H / 2 - values[i] * (SPARK_H / 2 - 4)]);
    const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
    const area = `${d}L${pts.at(-1)?.[0].toFixed(1) ?? 0},${SPARK_H / 2}L0,${SPARK_H / 2}Z`;
    const id = `clip-${key}`;
    const up = `up-${key}`;
    const down = `down-${key}`;
    const reveal = s("rect", { x: 0, y: 0, width: 0, height: SPARK_H });
    const marker = s("line", { class: "spark-now", y1: 0, y2: SPARK_H });
    const svg = s("svg", { class: "spark", viewBox: `0 0 ${SPARK_W} ${SPARK_H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
      s("defs", {},
        s("clipPath", { id }, reveal),
        s("clipPath", { id: up }, s("rect", { x: 0, y: 0, width: SPARK_W, height: SPARK_H / 2 })),
        s("clipPath", { id: down }, s("rect", { x: 0, y: SPARK_H / 2, width: SPARK_W, height: SPARK_H / 2 }))),
      s("line", { class: "spark-zero", x1: 0, x2: SPARK_W, y1: SPARK_H / 2, y2: SPARK_H / 2 }),
      tl.segments.filter((seg) => seg.kind !== "play").map((seg) => s("rect", { class: "spark-break", x: (seg.start_s / tl.duration) * SPARK_W, y: 0, width: ((seg.end_s - seg.start_s) / tl.duration) * SPARK_W, height: SPARK_H })),
      s("g", { "clip-path": `url(#${id})` },
        s("path", { d: area, fill: colourUp, "fill-opacity": 0.22, "clip-path": `url(#${up})` }),
        s("path", { d: area, fill: colourDown, "fill-opacity": 0.22, "clip-path": `url(#${down})` }),
        s("path", { d, class: "spark-line" })),
      marker);
    const value = h("span", { class: "spark-value" });
    const title = h("span", { class: "spark-title" });
    const top = h("span", { class: "spark-end top" });
    const bottom = h("span", { class: "spark-end bottom" });
    const box = h("div", { class: "spark-box" }, h("div", { class: "spark-head" }, title, value), h("div", { class: "spark-frame" }, svg, top, bottom));
    return { box, reveal, marker, value, title, top, bottom };
  }
  const momentum = sparkline("momentum", ctx.sn.list.map((x) => x.momentum), home.colours.accent, away.colours.accent);
  const chaos = sparkline("chaos", ctx.sn.list.map((x) => x.chaos), "var(--fault)", "var(--chalk-2)");
  sparks.append(momentum.box, chaos.box);

  function updateStats(frame) {
    const snap = frame.snap;
    const source = snap ? (windowMode === "window" ? snap.teams : snap.totals) : null;
    for (const [key, r] of Object.entries(stripRows)) {
      const hvNum = source?.[home.team_id]?.[key];
      const avNum = source?.[away.team_id]?.[key];
      setText(r.hv, hvNum == null ? "–" : `${fmt(hvNum, r.digits)}${r.unit}`);
      setText(r.av, avNum == null ? "–" : `${fmt(avNum, r.digits)}${r.unit}`);
      const total = (hvNum ?? 0) + (avNum ?? 0);
      const share = total > 0 ? (100 * (hvNum ?? 0)) / total : 50;
      r.hb.style.width = `${share}%`;
      r.ab.style.width = `${100 - share}%`;
      r.row.setAttribute("aria-label", `${r.label.textContent}: ${home.short_name} ${r.hv.textContent}, ${away.short_name} ${r.av.textContent}`);
    }
    const x = (frame.t / tl.duration) * SPARK_W;
    for (const sp of [momentum, chaos]) {
      sp.reveal.setAttribute("width", x.toFixed(1));
      sp.marker.setAttribute("x1", x.toFixed(1));
      sp.marker.setAttribute("x2", x.toFixed(1));
    }
    const m = snap?.momentum ?? 0;
    setText(momentum.value, snap ? `${m >= 0 ? "+" : ""}${m.toFixed(2)} ${m >= 0 ? home.short_name : away.short_name}` : "–");
    const c = snap?.chaos ?? 0;
    setText(chaos.value, snap ? `${c >= 0 ? "+" : ""}${c.toFixed(2)}` : "–");
  }

  // ---------------------------------------------------------------- pitch events
  let eventsKey = null;
  let focusKey = null;
  let hlKey = null;
  function mark(e, cls) {
    const c = accent(e.team_id);
    const p = e.start ?? e.location;
    switch (e.type) {
      case "pass":
        return s("g", { class: `ev ev-pass ${e.outcome} ${cls}`, style: `--c:${c}` },
          s("line", { x1: e.start.x, y1: e.start.y, x2: e.end.x, y2: e.end.y }), s("circle", { cx: e.end.x, cy: e.end.y, r: 0.45 }));
      case "shot":
        return s("circle", { class: `ev ev-shot ${e.outcome} ${cls}`, style: `--c:${c}`, cx: p.x, cy: p.y, r: 0.9 + 2.4 * Math.sqrt(e.xg) });
      case "tackle":
        return s("path", { class: `ev ev-tackle ${e.outcome} ${cls}`, style: `--c:${c}`, d: `M${p.x - 0.9},${p.y - 0.9}l1.8,1.8m0,-1.8l-1.8,1.8` });
      case "pressure":
        return s("circle", { class: `ev ev-pressure ${cls}`, style: `--c:${c}`, cx: p.x, cy: p.y, r: 0.55 });
      case "possession_change":
        return s("path", { class: `ev ev-turnover ${cls}`, style: `--c:${c}`, d: `M${p.x},${p.y - 1}l1,1l-1,1l-1,-1z` });
      default:
        return null;
    }
  }

  function updatePitch(frame) {
    const recent = eventsBetween(ctx.ev, frame.t - WINDOW_S, frame.t).filter((e) => shown.has(e.type));
    const fav = ctx.state.persona.startsWith("fav:") ? ctx.state.persona.slice(4) : null;
    const key = `${recent.length ? recent[0].event_id : -1}-${recent.length ? recent.at(-1).event_id : -1}-${[...shown].join()}-${fav}-${Math.floor(frame.t / 10)}`;
    if (key !== eventsKey) {
      eventsKey = key;
      gEvents.replaceChildren(...recent.map((e) => {
        const age = (frame.t - e.t) / WINDOW_S;
        const el = mark(e, fav && e.player_id === fav ? "fav" : "");
        if (el) el.style.opacity = String(Math.max(0.18, 1 - age * 0.82));
        return el;
      }));
    }
    const focus = ctx.state.focus;
    const fkey = focus ? [...focus.ids].join(",") : "";
    if (fkey !== focusKey) {
      focusKey = fkey;
      const marks = focus ? [...focus.ids].map((id) => ctx.ev.byId.get(id)).filter(Boolean).map((e) => {
        const el = mark(e, "focus");
        const p = e.start ?? e.location;
        return el && p ? s("g", {}, el, s("text", { class: "focus-id", x: p.x + 1.4, y: p.y - 1.2 }, `#${e.event_id}`)) : el;
      }) : [];
      gFocus.replaceChildren(...marks);
    }
    const hk = frame.highlights.map((x) => x.overlay.overlay_id).join(",");
    if (hk !== hlKey) {
      hlKey = hk;
      gHighlight.replaceChildren(...frame.highlights.map(({ overlay }) => highlightShape(overlay.highlight, accent(overlay.highlight.team_id))));
    }
    ballDot.setAttribute("cx", frame.ball.x.toFixed(2));
    ballDot.setAttribute("cy", frame.ball.y.toFixed(2));
  }

  // ---------------------------------------------------------------- compare personas
  let compareKey = null;
  function updateCompare(frame) {
    const { lang, persona } = ctx.state;
    const overlay = frame.lower?.overlay ?? frame.latest;
    const key = `${overlay?.overlay_id}|${lang}|${persona}`;
    if (key === compareKey) return;
    compareKey = key;
    clear(compareCard);
    if (!overlay) {
      compareCard.append(h("h2", { class: "card-title" }, tr(lang, "currentMoment")), h("p", { class: "muted" }, tr(lang, "noMoment")));
      return;
    }
    const team = overlay.team_id ? teams[overlay.team_id] : null;
    const favs = ctx.personas.filter((p) => p.kind === "favourite");
    const fav = favs.find((p) => p.persona === persona) ?? favs.find((p) => overlay.player_ids.includes(p.player_id))
      ?? favs.find((p) => p.team_id === overlay.team_id) ?? favs[0];
    const cards = [ctx.personas.find((p) => p.kind === "analyst"), ctx.personas.find((p) => p.kind === "casual"), fav].filter(Boolean);
    compareCard.append(
      h("div", { class: "card-head" },
        h("h2", { class: "card-title" }, `${tr(lang, "currentMoment")} · ${minuteLabel(overlay.clock_s)} ${kindLabel(lang, overlay.kind, overlay.status)}`,
          team ? h("span", { class: "title-team" }, h("span", { class: "team-dot", style: { background: team.colours.accent } }), team.name) : null),
        h("button", { type: "button", class: "btn-evidence", onclick: () => ctx.actions.openDrawer(overlay.overlay_id) }, `${tr(lang, "evidence")} ›`)),
      h("div", { class: "persona-grid" }, cards.map((p) => {
        const variant = variantFor(overlay, p.persona, lang);
        return h("article", { class: `persona-card ${p.persona === persona ? "selected" : ""}` },
          h("h3", { class: "persona-name" }, personaLabel(lang, p)),
          h("p", { class: "persona-text", lang, onclick: (e) => {
            const fig = e.target.closest(".fig");
            if (fig) ctx.actions.openDrawer(overlay.overlay_id, { refs: fig.dataset.refs.split(" ") });
          } }, figuredText(variant)),
          h("div", { class: "persona-foot" }, provenanceChips(overlay, lang, variant, ctx.honesty).slice(0, 1),
            variant.reason ? h("span", { class: "chip warn", title: variant.reason }, variant.reason.slice(0, 48)) : null));
      })));
  }

  // ---------------------------------------------------------------- usage
  const usage = ctx.run.usage;
  const showAt = Object.fromEntries(ctx.overlays.map((o) => [o.job_id, o.show_at_s]));
  let usageKey = null;
  function updateUsage(frame) {
    const lang = ctx.state.lang;
    const done = usage.filter((u) => (showAt[u.finding_id] ?? Infinity) <= frame.t);
    const key = `${done.length}|${lang}`;
    if (key === usageKey) return;
    usageKey = key;
    const sum = (rows) => ({
      calls: rows.filter((u) => u.status !== "refused").length,
      input: rows.reduce((a, u) => a + (u.input_tokens ?? 0), 0),
      output: rows.reduce((a, u) => a + (u.output_tokens ?? 0), 0),
      cost: rows.reduce((a, u) => a + (u.cost_usd ?? 0), 0),
    });
    const all = sum(usage);
    const now = sum(done);
    const roles = ["interpreter", "explainer", "verifier", "personalizer"];
    const estimate = ctx.honesty.costIsEstimate;
    clear(usageCard).append(
      h("div", { class: "card-head" }, h("h2", { class: "card-title" }, tr(lang, "usage")),
        h("span", { class: `chip ${estimate ? "muted" : "ok"}` }, estimate ? tr(lang, "estimate") : tr(lang, "measured"))),
      h("div", { class: "usage-grid" },
        [[tr(lang, "soFar"), now], [tr(lang, "total"), all]].map(([label, v]) => h("div", { class: "usage-col" },
          h("span", { class: "kicker" }, label),
          h("span", { class: "usage-big" }, `$${v.cost.toFixed(4)}`),
          h("span", { class: "usage-small" }, `${v.calls} ${tr(lang, "calls")} · ${v.input.toLocaleString()} ${tr(lang, "tokensIn")} · ${v.output.toLocaleString()} ${tr(lang, "tokensOut")}`)))),
      h("table", { class: "usage-table" },
        h("caption", { class: "kicker" }, tr(lang, "byRole")),
        h("tbody", {}, roles.map((role) => {
          const r = sum(usage.filter((u) => u.role === role));
          return h("tr", {}, h("th", { scope: "row" }, role), h("td", {}, `${r.calls}`), h("td", {}, `$${r.cost.toFixed(4)}`));
        }))));
  }

  // ---------------------------------------------------------------- key-moment timeline
  const markers = [];
  const playhead = h("div", { class: "tl-playhead" });
  function buildTimeline() {
    const lang = ctx.state.lang;
    const pct = (t) => `${(100 * t) / tl.duration}%`;
    const lanes = { moments: h("div", { class: "tl-lane" }), spells: h("div", { class: "tl-lane spells" }) };
    markers.length = 0;
    const spellEnds = ctx.overlays.filter((o) => o.status === "end");
    const rowsEnd = []; // overlapping spells go to separate rows
    for (const o of ctx.overlays) {
      const team = o.team_id ? teams[o.team_id] : null;
      const colour = team?.colours.accent ?? "var(--chalk-2)";
      const label = `${minuteLabel(o.clock_s)} ${kindLabel(lang, o.kind, o.status)}${team ? `, ${team.name}` : ""}`;
      if (o.status === "start") {
        const end = spellEnds.find((x) => x.kind === o.kind && x.team_id === o.team_id && x.show_at_s >= o.show_at_s);
        const until = Math.max(end ? end.show_at_s : o.hide_at_s, o.show_at_s + 20);
        let row = rowsEnd.findIndex((e) => e <= o.show_at_s);
        if (row < 0) row = rowsEnd.push(0) - 1;
        rowsEnd[row] = until + tl.duration * 0.06; // room for the label
        const el = h("button", { type: "button", class: `tl-spell k-${o.kind}`, title: label, "aria-label": label,
                                  style: { left: pct(o.show_at_s), width: pct(until - o.show_at_s), top: `${3 + row * SPELL_ROW}px`, "--c": colour },
                                  onclick: () => ctx.actions.seek(o.show_at_s) }, h("span", {}, kindLabel(lang, o.kind)));
        lanes.spells.append(el);
        markers.push({ el, t: o.show_at_s });
      } else if (o.status !== "end") {
        const el = h("button", { type: "button", class: `tl-mark k-${o.kind}`, title: label, "aria-label": label,
                                  style: { left: pct(o.show_at_s), "--c": colour }, onclick: () => ctx.actions.seek(o.show_at_s) });
        lanes.moments.append(el);
        markers.push({ el, t: o.show_at_s });
      }
    }
    const axis = h("div", { class: "tl-axis" }, [0, 15, 30, 45, 60, 75, 90].map((m) => {
      const t = m === 45 ? tl.toTimeline(2700, 1) : tl.toTimeline(m * 60, m > 45 ? 2 : 1);
      return h("span", { style: { left: pct(t) } }, m === 45 ? `45' ${tr(lang, "halfTime")}` : m === 90 ? `90' ${tr(lang, "fullTime")}` : `${m}'`);
    }));
    const breaks = tl.segments.filter((x) => x.kind !== "play").map((x) => h("div", { class: "tl-break", style: { left: pct(x.start_s), width: pct(x.end_s - x.start_s) } }));
    const rows = Math.max(1, rowsEnd.length);
    lanes.spells.style.height = `${6 + rows * SPELL_ROW}px`;
    const lanesHeight = SPELLS_TOP + 6 + rows * SPELL_ROW;
    const track = h("div", { class: "tl-track", style: { "--lanes-h": `${lanesHeight}px` } }, breaks, lanes.moments, lanes.spells, playhead, axis);
    clear(timelineCard).append(
      h("div", { class: "card-head" }, h("h2", { class: "card-title" }, tr(lang, "keyMoments")),
        h("span", { class: "legend-inline" }, h("span", { class: "tl-key goal" }), tr(lang, "k_goal"), h("span", { class: "tl-key chance" }), tr(lang, "k_big_chance"),
          h("span", { class: "tl-key spell" }), tr(lang, "spells"))),
      track);
  }

  function updateTimeline(frame) {
    playhead.style.left = `${(100 * frame.t) / tl.duration}%`;
    for (const m of markers) {
      const future = m.t > frame.t;
      if (m.el.classList.contains("future") !== future) m.el.classList.toggle("future", future);
    }
  }

  // ---------------------------------------------------------------- refresh / update
  function refresh() {
    const lang = ctx.state.lang;
    setText(pitchTitle, tr(lang, "pitchTitle", { s: WINDOW_S }));
    filters.replaceChildren(...TYPES.map((type) => h("button", {
      type: "button", class: `filter f-${type}`, "aria-pressed": String(shown.has(type)),
      onclick: (e) => {
        if (shown.has(type)) shown.delete(type);
        else shown.add(type);
        e.currentTarget.setAttribute("aria-pressed", String(shown.has(type)));
        eventsKey = null;
        ctx.actions.rerender();
      },
    }, tr(lang, TYPE_LABEL[type]))));
    setText(legend, tr(lang, "ballNote"));
    setText(statsTitle, tr(lang, "stats"));
    windowSeg.replaceChildren(...[["window", tr(lang, "lastMin", { m: 5 })], ["totals", tr(lang, "match")]].map(([mode, label]) =>
      h("button", { type: "button", "aria-pressed": String(windowMode === mode), onclick: () => { windowMode = mode; refresh(); ctx.actions.rerender(); } }, label)));
    stripRows.possession_pct.label.textContent = tr(lang, "possession");
    stripRows.field_tilt_pct.label.textContent = tr(lang, "fieldTilt");
    stripRows.pressures_per_min.label.textContent = tr(lang, "pressures");
    setText(momentum.title, tr(lang, "momentum"));
    setText(momentum.top, home.short_name);
    setText(momentum.bottom, away.short_name);
    setText(chaos.title, tr(lang, "chaos"));
    setText(chaos.top, tr(lang, "chaosEnd"));
    setText(chaos.bottom, tr(lang, "control"));
    buildTimeline();
    compareKey = null;
    usageKey = null;
    eventsKey = null;
  }

  function update(frame) {
    setText(scoreEl, `${frame.score.home} – ${frame.score.away}`);
    setText(clockEl, frame.label);
    updatePitch(frame);
    updateStats(frame);
    updateCompare(frame);
    updateUsage(frame);
    updateTimeline(frame);
  }

  refresh();
  return { update, refresh };
}
