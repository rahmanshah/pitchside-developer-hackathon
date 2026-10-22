// Loads the run index and one run bundle (plain JSON / JSONL files under data/).

export const DATA_BASE = "data/";

async function get(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return response;
}

export async function fetchJSON(url) {
  return (await get(url)).json();
}

export async function fetchJSONL(url) {
  const text = await (await get(url)).text();
  return text.split(/\r?\n/).filter((line) => line.trim()).map((line) => JSON.parse(line));
}

export async function loadIndex() {
  return fetchJSON(`${DATA_BASE}index.json`);
}

export function runUrl(slug, file) {
  return `${DATA_BASE}${encodeURIComponent(slug)}/${file}`;
}

export async function loadRun(slug) {
  const [manifest, overlay, events, stats, usage] = await Promise.all([
    fetchJSON(runUrl(slug, "manifest.json")),
    fetchJSON(runUrl(slug, "overlay.json")),
    fetchJSONL(runUrl(slug, "events.jsonl")),
    fetchJSONL(runUrl(slug, "stats.jsonl")),
    fetchJSONL(runUrl(slug, "usage.jsonl")).catch(() => []),
  ]);
  return { slug, manifest, overlay, events, stats, usage };
}
