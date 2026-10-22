// Interface strings. Overlay texts come from the bundle (written by the pipeline in each language); claims stay in
// English because that is what the checker verified, and the evidence drawer says so.

const STRINGS = {
  en: {
    tagline: "Synthetic match intelligence, verified",
    run: "Run", view: "View", broadcast: "Broadcast", analyst: "Analyst", audience: "Audience", language: "Language",
    play: "Play", pause: "Pause", prev: "Previous moment", next: "Next moment", time: "Match time", speed: "Speed",
    runInfo: "Run info", evidence: "Evidence", moments: "Moments", close: "Close",
    persona_analyst: "Analyst", persona_casual: "Casual fan", persona_fav: "Favourite: {name}",
    ends: "ends", minute: "{m}'",
    k_goal: "Goal", k_big_chance: "Big chance", k_momentum_shift: "Momentum", k_pressure_surge: "Pressing surge",
    k_siege: "Siege", k_chaos_spell: "Chaos spell", k_counter_attack: "Counter-attack",
    k_recap_half_time: "Half-time", k_recap_full_time: "Full-time",
    template: "Template", checked: "checked", verified: "verified", verifierSkipped: "verifier skipped",
    notVerified: "not model-verified", faultInjected: "fault injected", heldForReading: "held for reading",
    possession: "Possession", fieldTilt: "Field tilt", pressures: "Pressures / min", momentum: "Momentum",
    chaos: "Control ↔ chaos", control: "Control", chaosEnd: "Chaos", lastMin: "Last {m} min", match: "Match",
    stats: "Live stats", currentMoment: "Current moment", noMoment: "No moment yet: press play or pick one on the timeline.",
    keyMoments: "Key moments", spells: "Spells", pitchTitle: "Last {s} s of events",
    passes: "Passes", shots: "Shots", tackles: "Tackles", pressuresShort: "Pressures", turnovers: "Turnovers",
    usage: "Model usage", calls: "calls", tokensIn: "tokens in", tokensOut: "tokens out", cost: "cost",
    soFar: "so far", total: "whole run", estimate: "token estimates, not real spend", measured: "measured",
    byRole: "By agent role", ballNote: "Ball path from event locations (no tracking data).",
    halfTime: "HT", fullTime: "FT",
  },
  es: {
    tagline: "Inteligencia de partido sintética, verificada",
    run: "Partido", view: "Vista", broadcast: "Retransmisión", analyst: "Analista", audience: "Audiencia",
    language: "Idioma", play: "Reproducir", pause: "Pausa", prev: "Momento anterior", next: "Momento siguiente",
    time: "Tiempo de partido", speed: "Velocidad", runInfo: "Detalles", evidence: "Evidencia", moments: "Momentos",
    close: "Cerrar",
    persona_analyst: "Analista", persona_casual: "Aficionado", persona_fav: "Favorito: {name}",
    ends: "termina", minute: "{m}'",
    k_goal: "Gol", k_big_chance: "Ocasión clara", k_momentum_shift: "Impulso", k_pressure_surge: "Oleada de presión",
    k_siege: "Asedio", k_chaos_spell: "Fase de caos", k_counter_attack: "Contraataque",
    k_recap_half_time: "Descanso", k_recap_full_time: "Final",
    template: "Plantilla", checked: "comprobado", verified: "verificado",
    verifierSkipped: "verificador omitido", notVerified: "sin verificación del modelo", faultInjected: "fallo inyectado",
    heldForReading: "pausa para leer",
    possession: "Posesión", fieldTilt: "Inclinación del campo", pressures: "Presiones / min", momentum: "Impulso",
    chaos: "Control ↔ caos", control: "Control", chaosEnd: "Caos", lastMin: "Últimos {m} min", match: "Partido",
    stats: "Estadísticas en directo", currentMoment: "Momento actual", noMoment: "Aún no hay momentos: pulsa reproducir o elige uno en la línea de tiempo.",
    keyMoments: "Momentos clave", spells: "Fases", pitchTitle: "Eventos de los últimos {s} s",
    passes: "Pases", shots: "Tiros", tackles: "Entradas", pressuresShort: "Presiones", turnovers: "Pérdidas",
    usage: "Uso del modelo", calls: "llamadas", tokensIn: "tokens de entrada", tokensOut: "tokens de salida",
    cost: "coste", soFar: "hasta ahora", total: "toda la ejecución", estimate: "estimación de tokens, no gasto real",
    measured: "medido", byRole: "Por rol de agente",
    ballNote: "Trayectoria del balón a partir de los eventos (sin datos de seguimiento).",
    halfTime: "DES", fullTime: "FIN",
  },
};

export const LANGUAGE_NAMES = { en: "EN", es: "ES" };

export function t(lang, key, vars = {}) {
  const table = STRINGS[lang] ?? STRINGS.en;
  const text = table[key] ?? STRINGS.en[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (vars[k] ?? `{${k}}`));
}

export function kindLabel(lang, kind, status) {
  const base = t(lang, `k_${kind}`);
  return status === "end" ? `${base} ${t(lang, "ends")}` : base;
}

export function personaLabel(lang, persona) {
  if (persona.kind === "favourite") return t(lang, "persona_fav", { name: persona.name ?? persona.persona });
  return t(lang, `persona_${persona.kind}`);
}

// Fill static [data-i18n] text and [data-i18n-label] aria-labels.
export function applyStatic(root, lang) {
  document.documentElement.lang = lang;
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(lang, el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-label]")) el.setAttribute("aria-label", t(lang, el.dataset.i18nLabel));
}
