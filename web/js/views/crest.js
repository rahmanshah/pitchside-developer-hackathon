// Generated crests for invented clubs: a shield in the club's colours with a pattern and its initials.
import { s } from "./dom.js";

const SHIELD = "M20 1.5 L38 6.5 V22 C38 34 30.5 42 20 46.5 C9.5 42 2 34 2 22 V6.5 Z";
let uid = 0;

function pattern(kind, fill) {
  switch (kind) {
    case "stripes":
      return [8, 18, 28].map((x) => s("rect", { x, y: 0, width: 5, height: 48, fill }));
    case "hoops":
      return [10, 22, 34].map((y) => s("rect", { x: 0, y, width: 40, height: 5, fill }));
    case "chevron":
      return [s("path", { d: "M2 14 L20 26 L38 14 V21 L20 33 L2 21 Z", fill })];
    case "sash":
      return [s("path", { d: "M2 6 L10 4 L38 34 L34 40 Z", fill })];
    case "quarters":
      return [s("rect", { x: 20, y: 0, width: 20, height: 22, fill }), s("rect", { x: 0, y: 22, width: 20, height: 26, fill })];
    default:
      return [s("path", { d: SHIELD, fill: "none", stroke: fill, "stroke-width": 2.5, transform: "translate(20 24) scale(0.82) translate(-20 -24)" })];
  }
}

export function crest(team, { size = 32, label = true } = {}) {
  const id = `crest-${++uid}`;
  const { primary, secondary, text } = team.colours;
  return s("svg", { class: "crest", viewBox: "0 0 40 48", width: size, height: size * 1.2, role: "img",
                    "aria-label": `${team.name} crest (generated)` },
    s("defs", {}, s("clipPath", { id }, s("path", { d: SHIELD }))),
    s("path", { d: SHIELD, fill: primary }),
    s("g", { "clip-path": `url(#${id})`, opacity: 0.9 }, pattern(team.crest.pattern, secondary)),
    label ? s("text", { x: 20, y: 30, "text-anchor": "middle", "font-family": "Barlow Condensed, sans-serif",
                        "font-weight": 700, "font-size": 13, fill: text, stroke: primary, "stroke-width": 3,
                        "paint-order": "stroke" }, team.crest.initials) : null,
    s("path", { d: SHIELD, fill: "none", stroke: "rgba(255,255,255,0.55)", "stroke-width": 1.2 }),
  );
}
