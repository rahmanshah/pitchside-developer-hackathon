// Evidence drawer (provenance chain + audit trail for one overlay) and the run info sheet (the manifest).
import { kindLabel, personaLabel, t as tr } from "../i18n.js";
import { minuteLabel } from "../replay.js";
import { clear, h, mmss } from "./dom.js";
import { figuredText, variantFor } from "./text.js";

const MAX_CHIPS = 12;
const OUTCOME_TONE = {
  passed: "ok", brief: "ok", draft: "ok", texts: "ok", model: "ok",
  rejected: "fault", failed: "fault", skipped: "warn", template: "muted",
};
const BADGE_TEXT = {
  template: "Template (never sent to the agents)",
  template_fallback: "Template fallback",
  semantic_check_skipped: "Semantic check skipped",
  retried: "Explainer retried",
  persona_template: "Some persona texts are templates",
  fault_injected: "Fault injected on purpose",
  transport_retry: "Transport retry",
};
const RELATION = {
  triggering: "Triggering events",
  in_window: "Events in the window",
  player: "Player's events",
};

function closeOnBackdrop(dialog) {
  dialog.addEventListener("click", (e) => {
    if (e.target === dialog) dialog.close();
  });
}

function windowText(ev) {
  if (ev.window_start_s == null) return "";
  return ev.window_end_s > ev.window_start_s ? `${mmss(ev.window_start_s)}–${mmss(ev.window_end_s)}` : `at ${mmss(ev.window_start_s)}`;
}

export function createDrawer(dialog, ctx) {
  closeOnBackdrop(dialog);
  const { teams } = ctx;

  function eventChips(link) {
    const chips = link.event_ids.slice(0, MAX_CHIPS).map((id) => {
      const e = ctx.ev.byId.get(id);
      return h("button", { type: "button", class: "ev-chip", title: "Show on the pitch",
                           onclick: () => { dialog.close(); ctx.actions.focusEvents([id], e?.t); } },
        `#${id} ${e?.type?.replace("_", " ") ?? ""}`);
    });
    const more = link.count - chips.length;
    return [chips, more > 0 ? h("span", { class: "ev-more" }, `+${more} more`) : null];
  }

  function filterText(flt) {
    if (!flt) return "";
    const who = flt.player_id ? (ctx.players[flt.player_id]?.name ?? flt.player_id)
      : flt.team_id ? (teams[flt.team_id]?.short_name ?? flt.team_id) : "both teams";
    const span = flt.to_s > flt.from_s ? `${mmss(flt.from_s)}–${mmss(flt.to_s)}` : `at ${mmss(flt.from_s)}`;
    return `${flt.types.join(", ").replaceAll("_", " ")} · ${who} · ${span}`;
  }

  function evidenceRow(ev, highlighted) {
    const isPlayer = ev.kind === "player";
    const title = isPlayer ? `${ev.name} (${ev.involvement})` : ev.say;
    const detail = isPlayer ? (teams[ev.team_id]?.name ?? ev.team_id)
      : `${ev.metric} = ${ev.value}${ev.unit ? ` ${ev.unit}` : ""}${ev.team_id ? ` · ${teams[ev.team_id]?.short_name ?? ev.team_id}` : ""}`;
    const origin = ev.origin ? ctx.overlays.find((o) => o.job_id === ev.origin) : null;
    return h("li", { class: `evidence ${highlighted ? "hit" : ""}`, id: `ev-${ev.ref}` },
      h("div", { class: "evidence-head" },
        h("span", { class: "ref" }, ev.ref),
        h("span", { class: "say" }, ev.known === false ? "unknown reference" : title),
        h("span", { class: "chip muted" }, ev.source ?? "")),
      h("div", { class: "evidence-meta" },
        h("span", { class: "mono" }, detail),
        ev.window_start_s != null ? h("span", {}, `window ${windowText(ev)}`) : null,
        ev.baseline != null ? h("span", {}, `baseline ${ev.baseline}`) : null,
        origin ? h("button", { type: "button", class: "link-btn small", onclick: () => open(origin) },
          `from ${minuteLabel(origin.clock_s)} ${kindLabel(ctx.state.lang, origin.kind, origin.status)} ›`) : null),
      ev.events.map((link) => h("div", { class: `events rel-${link.relation}` },
        h("div", { class: "events-head" },
          h("span", { class: "rel" }, `${RELATION[link.relation] ?? link.relation} (${link.count})`),
          link.filter ? h("span", { class: "filter-text" }, filterText(link.filter)) : null,
          link.event_ids.length ? h("button", { type: "button", class: "link-btn small",
            onclick: () => { dialog.close(); ctx.actions.focusEvents(link.event_ids, ctx.ev.byId.get(link.event_ids.at(-1))?.t); } },
            "Show on pitch ›") : null),
        h("div", { class: "chips" }, eventChips(link)))));
  }

  function open(overlay, { refs = [] } = {}) {
    const lang = ctx.state.lang;
    const persona = ctx.state.persona;
    const team = overlay.team_id ? teams[overlay.team_id] : null;
    const p = overlay.provenance;
    const a = overlay.audit;
    const variant = variantFor(overlay, persona, lang);
    const personaInfo = ctx.personas.find((x) => x.persona === persona);
    const hits = new Set(refs);
    const semantic = p.semantic_check === "passed" ? h("span", { class: "chip ok" }, "✓ semantic verifier")
      : p.semantic_check.startsWith("skipped") ? h("span", { class: "chip warn" }, `⚠ ${p.semantic_check}`)
      : h("span", { class: "chip muted" }, "semantic verifier not run (template)");

    clear(dialog).append(h("div", { class: "drawer-inner" },
      h("header", { class: "drawer-head" },
        h("div", {},
          h("p", { class: "kicker" }, [minuteLabel(overlay.clock_s), kindLabel(lang, overlay.kind, overlay.status), team?.name].filter(Boolean).join(" · ")),
          h("h2", { id: "drawer-title" }, `${tr(lang, "evidence")}: ${overlay.headline}`)),
        h("button", { type: "button", class: "icon-btn close", "aria-label": tr(lang, "close"), onclick: () => dialog.close() }, "✕")),

      h("div", { class: "badges" },
        h("span", { class: `chip ${p.source === "model" ? "amber" : "muted"}` }, p.source === "model" ? "Model narrative" : `Template narrative${p.reason ? `: ${p.reason}` : ""}`),
        h("span", { class: "chip ok" }, "✓ deterministic checker"), semantic,
        a.badges.filter((b) => b !== "template" && b !== "template_fallback" && b !== "semantic_check_skipped")
          .map((b) => h("span", { class: `chip ${b === "fault_injected" ? "fault" : "warn"}` }, BADGE_TEXT[b] ?? b))),

      h("section", { class: "drawer-sec" },
        h("h3", {}, `Shown text · ${personaInfo ? personaLabel(lang, personaInfo) : persona} · ${lang.toUpperCase()}`),
        h("p", { class: "shown", lang }, figuredText(variant)),
        variant.source === "template" ? h("p", { class: "note" }, `Template text${variant.reason ? `: ${variant.reason}` : ""}`) : null,
        h("details", { class: "all-texts" }, h("summary", {}, "Every persona and language"),
          h("table", {}, h("tbody", {}, Object.entries(overlay.texts).flatMap(([pid, byLang]) => Object.entries(byLang).map(([code, v]) => {
            const info = ctx.personas.find((x) => x.persona === pid);
            return h("tr", {}, h("th", { scope: "row" }, `${info ? personaLabel(lang, info) : pid} · ${code}`),
              h("td", { lang: code }, v.text, " ", h("span", { class: `chip ${v.source === "model" ? "amber" : "muted"}` }, v.source)));
          })))))),

      h("section", { class: "drawer-sec" },
        h("h3", {}, `Written from ${p.claims.length} verified claim${p.claims.length === 1 ? "" : "s"}`),
        h("p", { class: "note" }, "Claims are shown in English, exactly as the checker verified them. Every number cites a ledger entry computed by the stats engine."),
        h("ol", { class: "claims" }, p.claims.map((c) => h("li", { class: "claim" },
          h("p", { class: "claim-text" }, c.text),
          h("div", { class: "chips" }, h("span", { class: "chip ok" }, "✓ checker"),
            c.semantic === "passed" ? h("span", { class: "chip ok" }, "✓ verifier")
              : c.semantic === "skipped" ? h("span", { class: "chip warn" }, "⚠ verifier skipped") : h("span", { class: "chip muted" }, "verifier not run")),
          h("ul", { class: "evidence-list" }, c.evidence.map((ev) => evidenceRow(ev, hits.has(ev.ref)))))))),

      h("section", { class: "drawer-sec" },
        h("h3", {}, "Audit trail"),
        h("ol", { class: "audit" }, a.steps.map((st) => h("li", { class: "step" },
          h("span", { class: "seq" }, st.seq),
          h("div", {},
            h("div", { class: "step-head" }, h("strong", {}, st.step), st.attempt ? h("span", { class: "muted-s" }, `#${st.attempt}`) : null,
              h("span", { class: "mono muted-s" }, st.actor), h("span", { class: `chip ${OUTCOME_TONE[st.outcome] ?? "muted"}` }, st.outcome),
              st.next_step ? h("span", { class: "muted-s" }, `→ ${st.next_step}`) : null),
            h("p", { class: "step-detail" }, st.detail),
            st.prompt_version ? h("span", { class: "mono muted-s" }, `prompt ${st.prompt_version}`) : null)))),
        a.rejected.length ? [h("h4", {}, `Rejected claims (${a.rejected.length})`),
          h("ul", { class: "rejected" }, a.rejected.map((r) => h("li", {},
            h("div", { class: "chips" }, h("span", { class: "chip muted" }, `draft ${r.attempt}`), h("span", { class: "chip fault" }, r.layer)),
            h("p", { class: "struck" }, r.text),
            h("ul", { class: "reasons" }, r.reasons.map((x) => h("li", {}, h("span", { class: "mono" }, x.code), ` ${x.detail}`))))))] : null,
        a.faults.length ? [h("h4", {}, "Injected faults (opt-in demo)"),
          h("ul", { class: "faults" }, a.faults.map((f) => h("li", {}, h("span", { class: "chip fault" }, `${f.role}: ${f.kind}`),
            ` ${f.step ?? ""}${f.attempt ? ` attempt ${f.attempt}` : ""}${f.count > 1 ? ` ×${f.count}` : ""}`)))] : null,
        h("h4", {}, "Model calls"),
        h("p", { class: "calls" }, `${a.calls.calls} calls · ${a.calls.errors} errors · ${a.calls.transport_retries} transport retries · ` +
          `${a.calls.input_tokens.toLocaleString()} tokens in · ${a.calls.output_tokens.toLocaleString()} out · $${a.calls.cost_usd.toFixed(5)}` +
          (ctx.honesty.costIsEstimate ? " (token estimates, not real spend)" : ""))),

      h("footer", { class: "drawer-foot" }, "Raw files: ",
        ["narratives.json", "audit.jsonl", "findings.json", "overlay.json"].map((f) =>
          h("a", { href: `${ctx.dataBase}${ctx.run.slug}/${f}`, target: "_blank", rel: "noopener" }, f)))));

    if (!dialog.open) dialog.showModal();
    const first = refs.length ? dialog.querySelector(`#ev-${CSS.escape(refs[0])}`) : null;
    if (first) first.scrollIntoView({ block: "center" });
    else dialog.querySelector(".drawer-inner").scrollTop = 0;
  }

  return { open, close: () => dialog.close() };
}

export function createRunInfo(dialog, ctx) {
  closeOnBackdrop(dialog);
  const m = ctx.run.manifest;
  const hon = ctx.honesty;

  function rows(pairs) {
    return h("dl", { class: "kv" }, pairs.filter(([, v]) => v != null && v !== "").flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
  }

  function open(section) {
    const pipe = m.pipeline;
    const fired = {};
    for (const f of m.faults.fired ?? []) {
      const key = `${f.role}: ${f.kind} (${f.job_id.split(":").slice(1).join(":")})`;
      fired[key] = (fired[key] ?? 0) + 1;
    }
    clear(dialog).append(h("div", { class: "sheet-inner" },
      h("header", { class: "drawer-head" },
        h("h2", { id: "runinfo-title" }, tr(ctx.state.lang, "runInfo")),
        h("button", { type: "button", class: "icon-btn close", "aria-label": tr(ctx.state.lang, "close"), onclick: () => dialog.close() }, "✕")),
      h("section", { class: "drawer-sec" },
        h("p", { class: `badge-static mode-${hon.mode}` }, hon.badge),
        h("blockquote", { class: "manifest-label" }, m.label),
        h("p", { class: "note" }, "This page replays a recorded run bundle. Nothing here calls a model: every text, check and number was produced when the run was recorded, and the label above is the bundle's own.")),
      h("section", { class: "drawer-sec" }, h("h3", {}, "Run"),
        rows([["Run id", m.run_id], ["Match", m.match_id], ["Recorded (UTC)", m.created_utc], ["Wall time", `${m.wall_s.toFixed(1)} s`],
              ["Pitchside", `${pipe.pitchside_version}${pipe.git_commit ? ` @ ${pipe.git_commit}` : ""}`],
              ["Generator", `seed ${m.generator.seed}, storyline ${m.generator.storyline}`], ["Storyline", m.generator.description],
              ["Final score", `${m.home.name} ${m.final_score.home}-${m.final_score.away} ${m.away.name}`],
              ["Clubs and players", "Invented (synthetic data); crest colours are generated"]])),
      h("section", { class: "drawer-sec" }, h("h3", {}, "Pipeline"),
        rows([["Model mode", pipe.llm_mode], ["API", pipe.api],
              ["Deployments", Object.entries(pipe.deployments).map(([k, v]) => `${k}: ${v}`).join(" · ")],
              ["Reasoning effort", pipe.reasoning_effort],
              ["Output caps", Object.entries(pipe.max_output_tokens).map(([k, v]) => `${k} ${v}`).join(" · ")],
              ["Prompt versions", Object.entries(pipe.prompt_versions).map(([k, v]) => `${k} ${v}`).join(" · ")],
              ["Explainer drafts (max)", String(pipe.max_explainer_attempts)],
              ["Stats tools", pipe.interpreter_tools + (pipe.mcp_server ? ` (${pipe.mcp_server})` : "")]])),
      h("section", { class: "drawer-sec" }, h("h3", {}, "Budget and counts"),
        rows([["Match budget", `$${m.budget.match_budget_usd}`], ["Spent", `$${m.budget.spent_usd.toFixed(5)}${hon.costIsEstimate ? " (token estimates, not real spend)" : ""}`],
              ["Model calls", String(m.budget.model_calls)], ["Budget fallbacks", String(m.budget.budget_fallbacks)],
              ["Events / findings", `${m.counts.events} / ${m.counts.findings}`],
              ["Jobs", `${m.counts.jobs} (model ${m.counts.model_jobs}, template ${m.counts.template_jobs}, model fallbacks ${m.counts.model_fallbacks}, recaps ${m.counts.recaps})`]])),
      h("section", { class: "drawer-sec", id: "runinfo-faults" }, h("h3", {}, "Injected faults"),
        m.faults.injected
          ? [h("p", { class: "note" }, `Faults were injected on purpose (plan "${m.faults.plan}") to show how the pipeline recovers. ${hon.faultsFired} fired.`),
             h("ul", { class: "faults" }, (m.faults.rules ?? []).map((r) => h("li", {}, r))),
             h("ul", { class: "faults fired" }, Object.entries(fired).map(([k, n]) => h("li", {}, h("span", { class: "chip fault" }, k), n > 1 ? ` ×${n}` : "")))]
          : h("p", { class: "note" }, "None: this run had no injected faults.")),
      h("section", { class: "drawer-sec" }, h("h3", {}, "Keyboard"),
        rows([["Space / K", "play or pause"], ["← →", "seek 10 s (Shift: 60 s)"], ["N / B", "next / previous moment"], ["[ ]", "slower / faster"],
              ["1 / 2", "broadcast / analyst view"], ["P / L", "next audience / language"], ["E", "evidence for the current moment"], ["I", "this panel"], ["Esc", "close"]])),
      h("footer", { class: "drawer-foot" }, "Raw files: ",
        ["manifest.json", "usage.jsonl", "answer_key.json"].map((f) => h("a", { href: `${ctx.dataBase}${ctx.run.slug}/${f}`, target: "_blank", rel: "noopener" }, f)))));
    if (!dialog.open) dialog.showModal();
    if (section === "faults") dialog.querySelector("#runinfo-faults")?.scrollIntoView({ block: "start" });
  }

  return { open, close: () => dialog.close() };
}
