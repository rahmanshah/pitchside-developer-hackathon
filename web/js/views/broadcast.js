// Broadcast view: a 16:9 simulated match feed with scoreboard, clock, honesty bug, moments feed and lower thirds,
// all timed by the overlay contract.
import { kindLabel, t as tr } from "../i18n.js";
import { minuteLabel } from "../replay.js";
import { lastAtOrBefore } from "../timeline.js";
import { crest } from "./crest.js";
import { clear, h, s, setText } from "./dom.js";
import { highlightShape, pitchSvg } from "./pitch.js";
import { figuredText, provenanceChips, variantFor } from "./text.js";

const TRAIL = 7;
const RECENT_S = 14;

export function createBroadcast(root, ctx) {
  const { home, away, teams } = ctx;
  const accent = (teamId) => teams[teamId]?.colours.accent ?? "var(--chalk-2)";

  const pitch = pitchSvg({ label: "Simulated match feed: ball path from event locations" });
  const layerRecent = s("g", { class: "recent" });
  const layerHighlight = s("g", { class: "highlights" });
  const trail = s("polyline", { class: "trail", points: "" });
  const ball = s("g", { class: "ball" }, s("circle", { class: "ball-glow", r: 2.2 }), s("circle", { class: "ball-dot", r: 0.9 }));
  const shirt = s("text", { class: "shirt", "text-anchor": "middle", dy: -2.2 });
  pitch.append(layerHighlight, layerRecent, trail, ball, shirt);

  const scoreHome = h("span", { class: "sb-score" }, "0");
  const scoreAway = h("span", { class: "sb-score" }, "0");
  const clockEl = h("span", { class: "sb-clock" }, "00:00");
  const periodEl = h("span", { class: "sb-period" }, "1H");
  const teamCell = (team) => h("span", { class: "sb-team", style: { "--team": team.colours.primary, "--team-text": team.colours.text } },
    crest(team, { size: 30 }), h("span", { class: "sb-code" }, team.short_name));
  const scorebug = h("div", { class: "scorebug", role: "status", "aria-label": "Score and clock" },
    teamCell(home), h("span", { class: "sb-result" }, scoreHome, h("span", { class: "sb-dash" }, "–"), scoreAway),
    teamCell(away), h("span", { class: "sb-time" }, clockEl, periodEl));

  const hon = ctx.honesty;
  const bug = h("div", { class: `corner-bug mode-${hon.mode}` },
    h("span", { class: "bug-line" }, h("span", { class: "bug-dot" }), hon.badge),
    hon.faultsInjected ? h("span", { class: "bug-fault" }, `◆ Faults injected on purpose · ${hon.faultsFired} fired`) : null);

  const feedTitle = h("h2", { class: "feed-title" });
  const feedList = h("ol", { class: "feed-list" });
  const feed = h("aside", { class: "feed" }, feedTitle, feedList);

  const lowerWrap = h("div", { class: "lt-wrap" });
  const note = h("p", { class: "stage-note" });

  const stage = h("div", { class: "stage" },
    h("div", { class: "stage-pitch" }, pitch), scorebug, bug, feed, lowerWrap, note);
  clear(root).append(stage);

  let lowerKey = null;
  let feedKey = null;
  let recentKey = null;
  let hlKey = null;

  function openFrom(event, overlayId) {
    const fig = event.target.closest?.(".fig");
    ctx.actions.openDrawer(overlayId, fig ? { refs: fig.dataset.refs.split(" ") } : {});
  }

  function renderLower(placement) {
    const { lang, persona } = ctx.state;
    const key = placement ? `${placement.overlay.overlay_id}|${persona}|${lang}` : null;
    if (key === lowerKey) return;
    lowerKey = key;
    clear(lowerWrap);
    if (!placement) return;
    const o = placement.overlay;
    const team = o.team_id ? teams[o.team_id] : null;
    const variant = variantFor(o, persona, lang);
    const label = kindLabel(lang, o.kind, o.status);
    const button = h("button", {
      type: "button", class: `lt kind-${o.kind} ${team ? "" : "neutral"}`,
      style: team ? { "--team": team.colours.primary, "--team-text": team.colours.text } : {},
      "aria-label": `${label}, ${minuteLabel(o.clock_s)}. ${variant.text}. ${tr(lang, "evidence")}`,
      onclick: (e) => openFrom(e, o.overlay_id),
    },
      h("span", { class: "lt-kicker" },
        team ? h("span", { class: "lt-team" }, crest(team, { size: 22, label: false }), team.short_name) : null,
        h("span", { class: "lt-kind" }, label),
        h("span", { class: "lt-min" }, o.status === "recap" ? "" : minuteLabel(o.clock_s))),
      h("span", { class: "lt-text", lang }, figuredText(variant)),
      h("span", { class: "lt-rail" }, provenanceChips(o, lang, variant),
        h("span", { class: "lt-evidence" }, `${tr(lang, "evidence")} ›`)));
    lowerWrap.append(button);
  }

  function renderFeed(side) {
    const { lang, persona } = ctx.state;
    const items = [...side].sort((a, b) => b.overlay.show_at_s - a.overlay.show_at_s).slice(0, 4);
    const key = `${items.map((x) => x.overlay.overlay_id).join(",")}|${persona}|${lang}`;
    if (key === feedKey) return;
    feedKey = key;
    setText(feedTitle, tr(lang, "moments"));
    feed.hidden = items.length === 0;
    feedList.replaceChildren(...items.map(({ overlay: o }) => {
      const team = o.team_id ? teams[o.team_id] : null;
      const favourite = persona.startsWith("fav:") && o.player_ids.includes(persona.slice(4));
      return h("li", {},
        h("button", { type: "button", class: `feed-card ${favourite ? "fav" : ""}`, style: { "--team": team?.colours.primary ?? "var(--line-strong)" },
                      onclick: (e) => openFrom(e, o.overlay_id) },
          h("span", { class: "feed-head" }, h("span", { class: "feed-min" }, minuteLabel(o.clock_s)),
            h("span", { class: "feed-kind" }, kindLabel(lang, o.kind, o.status)), favourite ? h("span", { class: "feed-star", "aria-label": "favourite player" }, "★") : null),
          h("span", { class: "feed-text", lang }, variantFor(o, persona, lang).text)));
    }));
  }

  function renderRecent(t) {
    const idx = ctx.ev;
    const hi = lastAtOrBefore(idx.locatedTimes, t);
    const key = `${hi}`;
    if (key !== recentKey) {
      recentKey = key;
      const points = [];
      for (let i = Math.max(0, hi - TRAIL + 1); i <= hi; i++) {
        const e = idx.located[i];
        const p = e.start ?? e.location;
        points.push(`${p.x.toFixed(2)},${p.y.toFixed(2)}`);
      }
      trail.dataset.base = points.join(" ");
      const marks = [];
      for (let i = hi; i >= 0 && idx.located[i].t > t - RECENT_S; i--) {
        const e = idx.located[i];
        const p = e.start ?? e.location;
        if (e.type === "shot") marks.push(s("circle", { class: `mk-shot ${e.outcome}`, cx: p.x, cy: p.y, r: 1.3 + 2 * Math.sqrt(e.xg), style: `--c:${accent(e.team_id)}` }));
        else if (e.type === "tackle") marks.push(s("circle", { class: "mk-tackle", cx: p.x, cy: p.y, r: 0.9, style: `--c:${accent(e.team_id)}` }));
      }
      layerRecent.replaceChildren(...marks);
    }
  }

  function renderHighlights(highlights) {
    const key = highlights.map((x) => x.overlay.overlay_id).join(",");
    if (key === hlKey) return;
    hlKey = key;
    layerHighlight.replaceChildren(...highlights.map(({ overlay }) => highlightShape(overlay.highlight, accent(overlay.highlight.team_id))));
  }

  function update(frame) {
    setText(scoreHome, String(frame.score.home));
    setText(scoreAway, String(frame.score.away));
    setText(clockEl, frame.label === "HT" || frame.label === "FT" ? "" : frame.label);
    setText(periodEl, frame.at.phase === "break" ? tr(ctx.state.lang, "halfTime")
      : frame.at.phase === "end" ? tr(ctx.state.lang, "fullTime") : `${frame.at.period}H`);
    stage.classList.toggle("holding", Boolean(frame.holding));
    renderRecent(frame.t);
    const b = frame.ball;
    const base = trail.dataset.base ?? "";
    trail.setAttribute("points", `${base} ${b.x.toFixed(2)},${b.y.toFixed(2)}`.trim());
    ball.setAttribute("transform", `translate(${b.x.toFixed(2)} ${b.y.toFixed(2)})`);
    const player = b.event?.player_id ? ctx.players[b.event.player_id] : null;
    shirt.setAttribute("x", b.x.toFixed(2));
    shirt.setAttribute("y", b.y.toFixed(2));
    shirt.style.setProperty("--c", player ? accent(player.team_id) : "var(--chalk)");
    setText(shirt, player ? String(player.shirt_number) : "");
    const favId = ctx.state.persona.startsWith("fav:") ? ctx.state.persona.slice(4) : null;
    shirt.classList.toggle("fav", Boolean(favId && b.event?.player_id === favId));
    renderHighlights(frame.highlights);
    renderFeed(frame.side);
    renderLower(frame.lower);
  }

  function refresh() {
    lowerKey = null;
    feedKey = null;
    setText(note, tr(ctx.state.lang, "ballNote"));
  }

  refresh();
  return { update, refresh };
}
