// Boot, state, controls and the render loop. Views are in ./views; the replay logic is in replay.js / timeline.js.
//
// URL parameters (demo video and tests): run, view=broadcast|analyst, persona=analyst|casual|fav|fav:<player_id>,
// lang=en|es, speed=<x>, autoplay=0|1, t=<timeline s>|<mm:ss>[@period], clean=1, drawer=<overlay_id>, motion=0,
// holds=0. A test hook is exposed as window.pitchside; <body data-ready="true"> once the first frame is drawn.

import { DATA_BASE, loadIndex, loadRun } from "./data.js";
import { honesty } from "./honesty.js";
import { LANGUAGE_NAMES, applyStatic, kindLabel, personaLabel, t as tr } from "./i18n.js";
import { ReplayClock, clockLabel, makeTimeline, minuteLabel, parseTime } from "./replay.js";
import {
  activePlacements, ballAt, indexEvents, indexSnapshots, latestStarted, lowerThirdAt, scoreAt, snapshotAt,
} from "./timeline.js";
import { createAnalyst } from "./views/analyst.js";
import { createBroadcast } from "./views/broadcast.js";
import { h, setText } from "./views/dom.js";
import { createDrawer, createRunInfo } from "./views/drawer.js";
import { variantFor } from "./views/text.js";

const SPEEDS = [1, 4, 16, 60];
const params = new URLSearchParams(location.search);
const $ = (id) => document.getElementById(id);

const state = {
  view: params.get("view") === "analyst" ? "analyst" : "broadcast",
  lang: "en",
  persona: "casual",
  speed: Number(params.get("speed")) > 0 ? Number(params.get("speed")) : 16,
  clean: params.get("clean") === "1",
  focus: null, // {ids: Set<number>, t}
  lastLower: null,
  dirty: true,
  lastT: -1,
};

let ctx = null;
let clock = null;
let views = {};
let drawer = null;
let runInfo = null;

function now() {
  return performance.now();
}

// ------------------------------------------------------------------ boot

async function boot() {
  if (params.get("motion") === "0") document.documentElement.classList.add("no-motion");
  if (state.clean) document.body.classList.add("clean");
  let index;
  try {
    index = await loadIndex();
    if (!index.runs?.length) throw new Error("no runs in data/index.json");
    const slug = index.runs.some((r) => r.slug === params.get("run")) ? params.get("run") : index.runs[0].slug;
    const run = await loadRun(slug);
    setup(index, run);
  } catch (error) {
    const box = $("load-error");
    box.hidden = false;
    box.textContent = `Could not load the recorded run: ${error.message}`;
    document.body.dataset.ready = "error";
    console.error(error);
  }
}

function setup(index, run) {
  const doc = run.overlay;
  const tl = makeTimeline(doc.timeline);
  const overlays = doc.overlays;
  const teams = Object.fromEntries(doc.teams.map((team) => [team.team_id, team]));
  const languages = doc.languages.length ? doc.languages : ["en"];
  const personas = doc.personas;
  ctx = {
    index, run, doc, tl, overlays, teams, personas, languages,
    home: doc.teams.find((x) => x.side === "home"), away: doc.teams.find((x) => x.side === "away"),
    ev: indexEvents(run.events, tl), sn: indexSnapshots(run.stats, tl),
    honesty: honesty(run.manifest, doc.source),
    players: Object.fromEntries([run.events[0]?.home, run.events[0]?.away].filter(Boolean)
      .flatMap((team) => team.players.map((p) => [p.player_id, { ...p, team_id: team.team_id }]))),
    state,
    actions: { seek, openDrawer, focusEvents, setView, rerender },
    dataBase: DATA_BASE,
  };
  state.lang = languages.includes(params.get("lang")) ? params.get("lang") : languages[0];
  state.persona = resolvePersona(params.get("persona")) ?? "casual";

  const holds = params.get("holds") === "0" ? [] : overlays.flatMap((o) =>
    o.placements.filter((p) => p.anchor === "lower_third").map((p) => ({ at: p.show_at_s, ms: o.hold_ms })));
  clock = new ReplayClock({ duration: tl.duration, speed: state.speed, holds });
  const start = parseTime(params.get("t"), tl);
  if (start != null) clock.seek(start, now());

  views.broadcast = createBroadcast($("view-broadcast"), ctx);
  views.analyst = createAnalyst($("view-analyst"), ctx);
  drawer = createDrawer($("drawer"), ctx);
  runInfo = createRunInfo($("runinfo"), ctx);

  buildControls();
  applyLanguage();
  setView(state.view, { silent: true });
  render(true);
  document.body.dataset.ready = "true";

  if (params.get("drawer")) openDrawer(params.get("drawer"));
  if (params.get("autoplay") === "1") play();
  requestAnimationFrame(loop);
}

function resolvePersona(value) {
  if (!value || !ctx) return null;
  const favs = ctx.personas.filter((p) => p.kind === "favourite");
  if (value === "fav") return favs[0]?.persona ?? null;
  if (value === "fav:home" || value === "fav:away") {
    const team = value.endsWith("home") ? ctx.home : ctx.away;
    return favs.find((p) => p.team_id === team.team_id)?.persona ?? null;
  }
  return ctx.personas.some((p) => p.persona === value) ? value : null;
}

// ------------------------------------------------------------------ controls

function buildControls() {
  const select = $("run-select");
  for (const run of ctx.index.runs) {
    const tag = run.llm_mode === "foundry" ? "recorded live run" : "offline sample";
    const faults = run.faults_injected ? " · faults injected" : "";
    select.append(h("option", { value: run.slug, selected: run.slug === ctx.run.slug },
      `${run.title} (${tag}${faults})`));
  }
  select.addEventListener("change", () => {
    const next = new URLSearchParams(location.search);
    next.set("run", select.value);
    next.delete("t");
    next.delete("drawer");
    location.search = next.toString();
  });

  for (const button of $("view-seg").querySelectorAll("button")) {
    button.addEventListener("click", () => setView(button.dataset.view));
  }

  $("persona-select").addEventListener("change", (e) => setPersona(e.target.value));

  const langSeg = $("lang-seg");
  for (const code of ctx.languages) {
    langSeg.append(h("button", { type: "button", dataset: { lang: code }, lang: code,
                                 onclick: () => setLanguage(code) }, LANGUAGE_NAMES[code] ?? code.toUpperCase()));
  }

  const speedSeg = $("speed-seg");
  const speeds = SPEEDS.includes(state.speed) ? SPEEDS : [...SPEEDS, state.speed].sort((a, b) => a - b);
  for (const speed of speeds) {
    speedSeg.append(h("button", { type: "button", dataset: { speed }, onclick: () => setSpeed(speed) }, `${speed}×`));
  }

  $("play-btn").addEventListener("click", () => toggle());
  $("prev-btn").addEventListener("click", () => jumpMoment(-1));
  $("next-btn").addEventListener("click", () => jumpMoment(1));
  $("info-btn").addEventListener("click", () => runInfo.open());
  $("mode-badge").addEventListener("click", () => runInfo.open());
  $("fault-badge").addEventListener("click", () => runInfo.open("faults"));

  const scrubber = $("scrubber");
  scrubber.max = String(Math.ceil(ctx.tl.duration));
  scrubber.addEventListener("input", () => seek(Number(scrubber.value)));
  buildScrubMarks();

  const hon = ctx.honesty;
  const badge = $("mode-badge");
  badge.textContent = hon.badge;
  badge.dataset.mode = hon.mode;
  badge.title = hon.label;
  const fault = $("fault-badge");
  fault.hidden = !hon.faultsInjected;
  fault.textContent = `Faults injected · ${hon.faultsFired} fired`;
  fault.title = "Faults were injected on purpose (opt-in) to show how the pipeline recovers";
  $("clean-bug").textContent = hon.badge + (hon.faultsInjected ? " · faults injected" : "");

  document.addEventListener("keydown", onKey);
}

function buildScrubMarks() {
  const marks = $("scrub-marks");
  const pct = (t) => `${(100 * t) / ctx.tl.duration}%`;
  marks.append(h("div", { class: "fill", id: "scrub-fill" }));
  for (const seg of ctx.tl.segments) {
    if (seg.kind !== "play") marks.append(h("div", { class: "band", style: { left: pct(seg.start_s), width: pct(seg.end_s - seg.start_s) } }));
  }
  for (const o of ctx.overlays) {
    if (o.anchor !== "lower_third" || o.status === "recap") continue;
    marks.append(h("div", { class: `mk ${o.kind === "goal" ? "goal" : ""}`, style: { left: pct(o.show_at_s) } }));
  }
}

function applyLanguage() {
  applyStatic(document, state.lang);
  for (const b of $("lang-seg").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
  const select = $("persona-select");
  select.replaceChildren(...ctx.personas.map((p) => {
    const team = p.team_id ? ` (${ctx.teams[p.team_id]?.short_name ?? p.team_id})` : "";
    return h("option", { value: p.persona, selected: p.persona === state.persona }, personaLabel(state.lang, p) + team);
  }));
  updatePlayLabel();
}

function updatePlayLabel() {
  const playing = clock?.playing;
  document.body.classList.toggle("playing", Boolean(playing));
  $("play-btn").setAttribute("aria-label", tr(state.lang, playing ? "pause" : "play"));
  for (const b of $("speed-seg").querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.dataset.speed) === clock.speed));
}

function syncUrl() {
  const next = new URLSearchParams(location.search);
  next.set("run", ctx.run.slug);
  next.set("view", state.view);
  next.set("persona", state.persona);
  next.set("lang", state.lang);
  next.set("speed", String(clock.speed));
  next.delete("t");
  next.delete("drawer");
  history.replaceState(null, "", `?${next.toString()}`);
}

// ------------------------------------------------------------------ actions

function play() {
  clock.play(now());
  updatePlayLabel();
}

function pause() {
  clock.pause(now());
  updatePlayLabel();
}

function toggle() {
  if (clock.playing) pause();
  else play();
}

function seek(t) {
  clock.seek(t, now());
  state.focus = null; // a highlighted event set lasts until the next seek
  state.dirty = true;
}

function setSpeed(speed) {
  clock.setSpeed(speed, now());
  updatePlayLabel();
  syncUrl();
}

function setView(view, { silent = false } = {}) {
  state.view = view === "analyst" ? "analyst" : "broadcast";
  $("view-broadcast").hidden = state.view !== "broadcast";
  $("view-analyst").hidden = state.view !== "analyst";
  document.body.classList.toggle("view-is-analyst", state.view === "analyst");
  for (const b of $("view-seg").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.view === state.view));
  state.dirty = true;
  if (!silent) syncUrl();
}

function setPersona(persona) {
  state.persona = persona;
  rerender();
  syncUrl();
}

function setLanguage(lang) {
  state.lang = lang;
  applyLanguage();
  rerender();
  syncUrl();
}

function rerender() {
  views.broadcast.refresh();
  views.analyst.refresh();
  state.dirty = true;
}

function jumpMoment(direction) {
  const t = clock.time(now());
  const starts = [...new Set(ctx.overlays.flatMap((o) => o.placements.filter((p) => p.anchor === "lower_third")
    .map((p) => p.show_at_s)))].sort((a, b) => a - b);
  const target = direction > 0 ? starts.find((s) => s > t + 0.5) : [...starts].reverse().find((s) => s < t - 1);
  if (target != null) seek(target);
}

function openDrawer(overlayId, options = {}) {
  const overlay = ctx.overlays.find((o) => o.overlay_id === overlayId);
  if (overlay) drawer.open(overlay, options);
}

// Show some events on the analyst pitch: pause there and keep them highlighted.
function focusEvents(ids, t) {
  pause();
  if (t != null) seek(t);
  state.focus = { ids: new Set(ids), t };
  setView("analyst");
}

function onKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (document.querySelector("dialog[open]")) return;
  const tag = e.target?.tagName;
  const inField = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  const key = e.key;
  const step = e.shiftKey ? 60 : 10;
  if ((key === " " && tag !== "BUTTON" && !inField) || key === "k") {
    e.preventDefault();
    toggle();
  } else if (inField) {
    return;
  } else if (key === "ArrowLeft" || key === "ArrowRight") {
    e.preventDefault();
    seek(clock.time(now()) + (key === "ArrowRight" ? step : -step));
  } else if (key === "[" || key === "]") {
    const i = SPEEDS.indexOf(clock.speed);
    const next = key === "]" ? SPEEDS[Math.min(SPEEDS.length - 1, i + 1)] : SPEEDS[Math.max(0, i < 0 ? 0 : i - 1)];
    setSpeed(next);
  } else if (key === "1" || key === "2") {
    setView(key === "1" ? "broadcast" : "analyst");
  } else if (key === "p") {
    const i = ctx.personas.findIndex((p) => p.persona === state.persona);
    setPersona(ctx.personas[(i + 1) % ctx.personas.length].persona);
    $("persona-select").value = state.persona;
  } else if (key === "l") {
    const i = ctx.languages.indexOf(state.lang);
    setLanguage(ctx.languages[(i + 1) % ctx.languages.length]);
  } else if (key === "e") {
    const t = clock.time(now());
    const current = lowerThirdAt(ctx.overlays, t)?.overlay ?? latestStarted(ctx.overlays, t);
    if (current) openDrawer(current.overlay_id);
  } else if (key === "n" || key === "b") {
    jumpMoment(key === "n" ? 1 : -1);
  } else if (key === "i") {
    runInfo.open();
  }
}

// ------------------------------------------------------------------ render

function frameAt(t) {
  const at = ctx.tl.at(t);
  return {
    t, at, label: clockLabel(at), score: scoreAt(ctx.ev, t), snap: snapshotAt(ctx.sn, t),
    lower: lowerThirdAt(ctx.overlays, t), side: activePlacements(ctx.overlays, t, "side_panel"),
    highlights: activePlacements(ctx.overlays, t, "pitch_highlight"), ball: ballAt(ctx.ev, t),
    latest: latestStarted(ctx.overlays, t), holding: clock.holding(now()),
  };
}

function render(force = false) {
  const t = clock.time(now());
  if (!force && !state.dirty && t === state.lastT) return;
  state.dirty = false;
  state.lastT = t;
  const frame = frameAt(t);
  views[state.view].update(frame);
  setText($("time-label"), frame.label);
  const scrubber = $("scrubber");
  if (document.activeElement !== scrubber || !clock.playing) scrubber.value = String(Math.round(t));
  scrubber.setAttribute("aria-valuetext", `${frame.label}, ${ctx.home.short_name} ${frame.score.home}-${frame.score.away} ${ctx.away.short_name}`);
  $("scrub-fill").style.width = `${(100 * t) / ctx.tl.duration}%`;
  announce(frame);
}

function announce(frame) {
  const id = frame.lower?.overlay.overlay_id ?? null;
  if (id === state.lastLower) return;
  state.lastLower = id;
  if (!id) return;
  const o = frame.lower.overlay;
  const team = o.team_id ? ctx.teams[o.team_id]?.name ?? "" : "";
  const text = variantFor(o, state.persona, state.lang).text;
  setText($("announcer"), `${minuteLabel(o.clock_s)} ${kindLabel(state.lang, o.kind, o.status)} ${team}: ${text}`);
}

function loop() {
  if (clock.playing && clock.ended(now())) pause();
  render();
  requestAnimationFrame(loop);
}

// ------------------------------------------------------------------ test hook

window.pitchside = {
  seek(value) {
    const t = typeof value === "number" ? value : parseTime(value, ctx.tl);
    if (t != null) seek(t);
    render(true);
    return this.state();
  },
  play() { play(); },
  pause() { pause(); render(true); },
  setSpeed(speed) { setSpeed(speed); },
  state() {
    const t = clock.time(now());
    const frame = frameAt(t);
    return {
      run: ctx.run.slug, t, clock: frame.at.clock, label: frame.label, phase: frame.at.phase, score: frame.score,
      playing: clock.playing, speed: clock.speed, view: state.view, persona: state.persona, lang: state.lang,
      lowerThird: frame.lower?.overlay.overlay_id ?? null, honesty: ctx.honesty,
    };
  },
  overlays() { return ctx.overlays.map((o) => ({ id: o.overlay_id, kind: o.kind, show: o.show_at_s, hide: o.hide_at_s, placements: o.placements })); },
};

boot();
