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

export function provenanceChips(overlay, lang, variant) {
  const p = overlay.provenance;
  const badges = overlay.audit.badges;
  const chips = [];
  const source = variant?.source ?? p.source;
  chips.push(h("span", { class: `chip ${source === "model" ? "amber" : "muted"}` },
    source === "model" ? t(lang, "model") : t(lang, "template")));
  chips.push(h("span", { class: "chip ok" }, `✓ ${t(lang, "checked")}`));
  if (p.semantic_check === "passed") chips.push(h("span", { class: "chip ok" }, `✓ ${t(lang, "verified")}`));
  else if (p.semantic_check.startsWith("skipped")) chips.push(h("span", { class: "chip warn" }, `⚠ ${t(lang, "verifierSkipped")}`));
  else chips.push(h("span", { class: "chip muted" }, t(lang, "notVerified")));
  if (badges.includes("fault_injected")) chips.push(h("span", { class: "chip fault" }, `◆ ${t(lang, "faultInjected")}`));
  return chips;
}
