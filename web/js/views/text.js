// Persona texts with their verified figures marked, and the provenance rail under each overlay.
import { h } from "./dom.js";
import { t } from "../i18n.js";

export function variantFor(overlay, persona, lang) {
  const byLang = overlay.texts[persona] ?? overlay.texts.casual ?? Object.values(overlay.texts)[0] ?? {};
  return byLang[lang] ?? byLang.en ?? Object.values(byLang)[0] ?? { text: overlay.headline, numbers: [], source: "template" };
}

// The text as nodes: each verified figure is a span.fig carrying its ledger refs (click -> evidence).
export function figuredText(variant) {
  const out = [];
  let at = 0;
  for (const span of variant.numbers ?? []) {
    if (span.start < at) continue;
    out.push(variant.text.slice(at, span.start));
    out.push(h("span", { class: "fig", dataset: { refs: span.refs.join(" ") }, title: span.refs.join(", ") },
      variant.text.slice(span.start, span.end)));
    at = span.end;
  }
  out.push(variant.text.slice(at));
  return out;
}

// Label for who wrote a text: the honesty sourceLabels (deployment name for live runs, "Offline stand-in" for the
// fake client), or "Template". Persona texts are written by the personalizer, narratives by the explainer.
export function sourceChip(source, honesty, role, lang) {
  if (source !== "model") return h("span", { class: "chip muted src-template" }, t(lang, "template"));
  const live = honesty.mode === "foundry";
  return h("span", { class: `chip ${live ? "amber src-live" : "standin src-standin"}` }, honesty.sourceLabels[role]);
}

export function provenanceChips(overlay, lang, variant, honesty) {
  const p = overlay.provenance;
  const badges = overlay.audit.badges;
  const chips = [];
  chips.push(sourceChip(variant?.source ?? p.source, honesty, variant ? "personalizer" : "explainer", lang));
  chips.push(h("span", { class: "chip ok" }, `✓ ${t(lang, "checked")}`));
  if (p.semantic_check === "passed") chips.push(h("span", { class: "chip ok" }, `✓ ${t(lang, "verified")}`));
  else if (p.semantic_check.startsWith("skipped")) chips.push(h("span", { class: "chip warn" }, `⚠ ${t(lang, "verifierSkipped")}`));
  else chips.push(h("span", { class: "chip muted" }, t(lang, "notVerified")));
  if (badges.includes("fault_injected")) chips.push(h("span", { class: "chip fault" }, `◆ ${t(lang, "faultInjected")}`));
  return chips;
}
