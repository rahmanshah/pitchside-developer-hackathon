// A 105 x 68 m pitch in SVG user units (metres). The home side always attacks towards x = 105.
import { s } from "./dom.js";

export const L = 105;
export const W = 68;

export function pitchSvg({ margin = 4, stripes = 14, cls = "pitch-svg", label = "Pitch" } = {}) {
  const svg = s("svg", { class: cls, viewBox: `${-margin} ${-margin} ${L + 2 * margin} ${W + 2 * margin}`,
                         role: "img", "aria-label": label, preserveAspectRatio: "xMidYMid meet" });
  const turf = s("g", { class: "turf" },
    s("rect", { x: -margin, y: -margin, width: L + 2 * margin, height: W + 2 * margin, class: "surround" }),
    s("rect", { x: 0, y: 0, width: L, height: W, class: "grass" }),
    Array.from({ length: stripes }, (_, i) =>
      i % 2 ? s("rect", { x: (L / stripes) * i, y: 0, width: L / stripes, height: W, class: "stripe" }) : null),
  );
  const line = { class: "line", fill: "none" };
  const box = (x, depth, width) => s("rect", { ...line, x: x === 0 ? 0 : L - depth, y: (W - width) / 2, width: depth, height: width });
  const arc = (x, dir) => s("path", { ...line, d: `M ${x + dir * 16.5} ${W / 2 - 7.3} A 9.15 9.15 0 0 ${dir > 0 ? 1 : 0} ${x + dir * 16.5} ${W / 2 + 7.3}` });
  const goal = (x, dir) => s("rect", { class: "goal", x: dir > 0 ? x : x - 1.8, y: W / 2 - 3.66, width: 1.8, height: 7.32 });
  const markings = s("g", { class: "markings" },
    s("rect", { ...line, x: 0, y: 0, width: L, height: W }),
    s("line", { ...line, x1: L / 2, y1: 0, x2: L / 2, y2: W }),
    s("circle", { ...line, cx: L / 2, cy: W / 2, r: 9.15 }),
    s("circle", { class: "spot", cx: L / 2, cy: W / 2, r: 0.4 }),
    box(0, 16.5, 40.32), box(L, 16.5, 40.32), box(0, 5.5, 18.32), box(L, 5.5, 18.32),
    s("circle", { class: "spot", cx: 11, cy: W / 2, r: 0.35 }), s("circle", { class: "spot", cx: L - 11, cy: W / 2, r: 0.35 }),
    arc(0, 1), arc(L, -1), goal(L, 1), goal(0, -1),
    [[0, 0, "M 0 1 A 1 1 0 0 0 1 0"], [L, 0, `M ${L - 1} 0 A 1 1 0 0 0 ${L} 1`], [0, W, `M 1 ${W} A 1 1 0 0 0 0 ${W - 1}`],
     [L, W, `M ${L} ${W - 1} A 1 1 0 0 0 ${L - 1} ${W}`]].map(([, , d]) => s("path", { ...line, d })),
  );
  svg.append(turf, markings);
  return svg;
}

// Arrow / zone for an overlay's pitch highlight (docs/OVERLAY.md).
export function highlightShape(highlight, colour) {
  if (!highlight) return null;
  if (highlight.shape === "zone") {
    const x = Math.min(highlight.x, highlight.x2);
    return s("rect", { class: "hl-zone", x, y: highlight.y, width: Math.abs(highlight.x2 - highlight.x),
                       height: highlight.y2 - highlight.y, style: `--team:${colour}` });
  }
  return s("g", { class: "hl-arrow" },
    s("line", { x1: highlight.x, y1: highlight.y, x2: highlight.x2, y2: highlight.y2 }),
    s("circle", { cx: highlight.x, cy: highlight.y, r: 1.4 }),
  );
}
