// Tiny DOM builders. h("div", {class: "x", onclick: fn, dataset: {id: 1}}, "text", child, [more]).

const SVG_NS = "http://www.w3.org/2000/svg";

function build(el, attrs, children) {
  for (const [key, value] of Object.entries(attrs ?? {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "style" && typeof value === "object") {
      // Object.assign would drop custom properties (--team ...); setProperty handles both kinds.
      for (const [prop, v] of Object.entries(value)) {
        if (v == null) continue;
        if (prop.startsWith("--")) el.style.setProperty(prop, v);
        else el.style[prop] = v;
      }
    }
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "text") el.textContent = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function h(tag, attrs, ...children) {
  return build(document.createElement(tag), attrs, children);
}

export function s(tag, attrs, ...children) {
  return build(document.createElementNS(SVG_NS, tag), attrs, children);
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

// Set text only when it changes (cheap per-frame updates).
export function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

export function fmt(value, digits = 0) {
  return value == null || Number.isNaN(value) ? "–" : Number(value).toFixed(digits);
}

export function mmss(clock) {
  const whole = Math.max(0, Math.floor(clock));
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}
