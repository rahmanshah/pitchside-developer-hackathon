"""Pre-submission checks: run before tagging submission-v1, after the live bundles are published.

    uv run python scripts/check_submission.py            # exit 0 only if every check passes
    uv run python scripts/check_submission.py --root DIR # check another checkout (the tests use small fixtures)

Checks, each printed as PASS or FAIL with the reasons:

1. placeholders: no ``{{`` (a ``{{name}}`` or ``{{PENDING ...}}``) is left in docs/PITCH.md or README.md.
2. published runs: every run in web/data is a live run (manifest ``pipeline.llm_mode`` is ``foundry``).
3. README claims: a README line that claims a deployment or a live model (not marked planned, pending, offline...)
   is backed by the published manifests: live runs, and for Container Apps / the MCP server, runs recorded through a
   deployed MCP server (``pipeline.mcp_server`` set). The claim detection is a line heuristic; read each reported line.
4. run labels: each run's index entry, manifest and overlay agree on the model mode, the label and injected faults,
   so no run is labelled offline or has faults injected without the index saying so.
5. links: every relative link in README.md and docs/PITCH.md resolves to a file in the repo.
6. names: no "Premier League" and no listed real club or player name in the pitch or the UI strings (web/index.html,
   web/js, the published index, manifests and overlay texts), and every club and player name in the published runs
   is one of the invented ones from ``pitchside.teams``. The real-name list is a guard, not an exhaustive register.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pitchside.teams import CLUBS, build_team  # noqa: E402

DOCS = ("README.md", "docs/PITCH.md")
LIVE_MODE = "foundry"

# A README sentence is a claim unless it says it is not (yet) true, or describes a capability or the offline path.
# Text in double quotes is a mention (e.g. a UI label), not a claim, and is ignored.
NOT_A_CLAIM = re.compile(
    r"\bnot\b|\bno\b|\bplanned\b|\bpending\b|\buntil\b|\bwill\b|\bready\b|\bestimate|offline|\bfake\b|"
    r"\bcan\b|\bcould\b|\bwould\b|\bif\b|\bwhen\b|\bonce\b|\bmilestone\b", re.I)
DEPLOY_CLAIM = re.compile(r"Container Apps|Static Web Apps?\b|\bdeployed\b", re.I)
MCP_CLAIM = re.compile(r"Container Apps|MCP server", re.I)
LIVE_CLAIM = re.compile(r"live pipeline|GPT-6 Luna|\bFoundry\b|\blive (?:runs?|bundles?|recordings?)\b", re.I)

# Real clubs (current and recent top-flight English sides) and well-known players. Matched case-sensitively as
# whole phrases, so ordinary words are not caught. Players are matched by full name, plus a few unmistakable
# surnames: the invented squads keep some common surnames (e.g. Petrovic, Esposito) that real players also have.
REAL_CLUBS = (
    "Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton & Hove Albion", "Brighton and Hove Albion",
    "Burnley", "Chelsea", "Crystal Palace", "Everton", "Fulham", "Leeds United", "Liverpool", "Manchester City",
    "Manchester United", "Man City", "Man Utd", "Newcastle United", "Nottingham Forest", "Sunderland",
    "Tottenham Hotspur", "Tottenham", "Spurs", "West Ham United", "West Ham", "Wolverhampton Wanderers",
    "Leicester City", "Ipswich Town", "Southampton", "Sheffield United", "Luton Town", "Watford", "Norwich City",
    "West Bromwich Albion", "Middlesbrough", "Stoke City", "Swansea City", "Cardiff City", "Hull City",
    "Blackburn Rovers", "Bolton Wanderers", "Queens Park Rangers", "Wigan Athletic", "Huddersfield Town",
)
REAL_PLAYERS = (
    "Erling Haaland", "Mohamed Salah", "Harry Kane", "Bukayo Saka", "Cole Palmer", "Son Heung-min",
    "Kevin De Bruyne", "Virgil van Dijk", "Marcus Rashford", "Bruno Fernandes", "Martin Ødegaard", "Martin Odegaard",
    "Declan Rice", "Phil Foden", "Alexander Isak", "Ollie Watkins", "Jarrod Bowen", "Trent Alexander-Arnold",
    "Raheem Sterling", "Jamie Vardy", "Timo Werner", "Diego Costa", "Craig Bellamy", "Luis Suárez", "Luis Suarez",
    "Gareth Bale", "Cristiano Ronaldo", "Sergio Agüero", "Sergio Aguero", "Alan Shearer", "Thierry Henry",
    "Wayne Rooney", "Steven Gerrard", "Frank Lampard", "Didier Drogba", "Eric Cantona", "Ryan Giggs", "Paul Scholes",
    "David Beckham", "Dennis Bergkamp", "Patrick Vieira", "Roy Keane", "John Terry", "Rio Ferdinand", "Michael Owen",
    "Robbie Fowler", "Gary Lineker", "Peter Crouch", "Jermain Defoe",
    "Haaland", "Salah", "Saka", "Ødegaard", "Odegaard", "De Bruyne", "Shearer", "Lineker", "Drogba", "Cantona",
)
REAL_NAMES = re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, REAL_CLUBS + REAL_PLAYERS)) + r")(?![\w-])")
LEAGUE = re.compile(r"premier\s+league", re.I)


@dataclass
class Result:
    name: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path):
    return json.loads(_read(path))


def _without_code(text: str) -> list[tuple[int, str]]:
    """(line number, line) for prose lines: fenced code blocks dropped, inline code blanked."""
    lines, fenced = [], False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append((number, re.sub(r"`[^`]*`", "``", line)))
    return lines


BLOCK_START = re.compile(r"^\s*(?:[-*+] |\d+\. |\|)")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z*(\[])")


def _sentences(text: str) -> list[tuple[int, str]]:
    """(line number, sentence) for the prose: wrapped lines joined, headings skipped, list items and table rows apart.

    The line number is the line the sentence starts on.
    """
    blocks: list[list[tuple[int, str]]] = []  # each block: its lines, in order
    for number, line in _without_code(text):
        if not line.strip() or line.lstrip().startswith("#"):
            blocks.append([])
        elif BLOCK_START.match(line) or not blocks or not blocks[-1] or blocks[-1][0][1].startswith("|"):
            blocks.append([(number, line.strip())])
        else:
            blocks[-1].append((number, line.strip()))
    sentences = []
    for block in filter(None, blocks):
        joined, starts = "", []  # starts: (offset in joined, line number)
        for number, part in block:
            starts.append((len(joined), number))
            joined += part + " "
        begin = 0
        for match in [*SENTENCE_END.finditer(joined), None]:
            end = match.start() if match else len(joined)
            if joined[begin:end].strip():
                sentences.append((max(n for offset, n in starts if offset <= begin), joined[begin:end].strip()))
            begin = match.end() if match else end
    return sentences


def _runs(root: Path) -> tuple[list[dict], list[str]]:
    """The published index entries, and any problem reading them."""
    path = root / "web" / "data" / "index.json"
    if not path.exists():
        return [], [f"{path.relative_to(root)} is missing"]
    try:
        return list(_json(path).get("runs", [])), []
    except (ValueError, AttributeError) as error:
        return [], [f"{path.relative_to(root)} cannot be read: {error}"]


def _manifest(root: Path, slug: str) -> dict | None:
    path = root / "web" / "data" / slug / "manifest.json"
    try:
        return _json(path)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------- checks

def check_placeholders(root: Path) -> Result:
    found = []
    for doc in DOCS:
        path = root / doc
        if path.exists():
            found += [f"{doc}:{n}: {line.strip()[:110]}" for n, line in enumerate(_read(path).splitlines(), 1)
                      if "{{" in line]
    return Result("placeholders", not found,
                  f"{len(found)} line(s) with {{{{ left" if found else f"no {{{{ left in {', '.join(DOCS)}", found)


def check_published_runs_live(root: Path) -> Result:
    runs, problems = _runs(root)
    if problems or not runs:
        return Result("published runs", False, "no published runs to check", problems or ["the index lists no runs"])
    offline = []
    for run in runs:
        m = _manifest(root, run["slug"])
        mode = m.get("pipeline", {}).get("llm_mode") if m else None
        if mode != LIVE_MODE:
            offline.append(f"{run['slug']}: llm_mode {mode!r}" + ("" if m else " (manifest missing or unreadable)"))
    return Result("published runs", not offline,
                  f"{len(offline)} of {len(runs)} published run(s) are not live (llm_mode {LIVE_MODE})" if offline
                  else f"all {len(runs)} published runs are live (llm_mode {LIVE_MODE})", offline)


def check_readme_claims(root: Path) -> Result:
    readme = root / "README.md"
    if not readme.exists():
        return Result("README claims", False, "README.md is missing")
    runs, problems = _runs(root)
    manifests = {r["slug"]: _manifest(root, r["slug"]) or {} for r in runs}
    not_live = [s for s, m in manifests.items() if m.get("pipeline", {}).get("llm_mode") != LIVE_MODE]
    no_mcp = [s for s, m in manifests.items() if not str(m.get("pipeline", {}).get("mcp_server") or "").startswith(
        "https://") or m.get("pipeline", {}).get("interpreter_tools") != "mcp"]
    unsupported = list(problems)
    for number, sentence in _sentences(_read(readme)):
        claim = re.sub(r"\"[^\"]*\"|“[^”]*”", "\"\"", sentence)
        if NOT_A_CLAIM.search(claim):
            continue
        needs = []
        if (DEPLOY_CLAIM.search(claim) or LIVE_CLAIM.search(claim)) and (not_live or not runs):
            needs.append(f"live runs (not live: {', '.join(not_live) or 'no runs published'})")
        if DEPLOY_CLAIM.search(claim) and MCP_CLAIM.search(claim) and (no_mcp or not runs):
            needs.append(f"runs recorded through a deployed MCP server (missing: {', '.join(no_mcp) or 'no runs'})")
        if needs:
            unsupported.append(f"README.md:{number}: {sentence[:100]} -> needs {'; '.join(needs)}")
    return Result("README claims", not unsupported,
                  f"{len(unsupported)} claim(s) not backed by the published manifests" if unsupported
                  else "every deployment or live-model claim is backed by the published manifests", unsupported)


def check_run_labels(root: Path) -> Result:
    runs, problems = _runs(root)
    issues = list(problems)
    data = root / "web" / "data"
    listed = {r.get("slug") for r in runs}
    if data.exists():
        issues += [f"{p.name}: a run directory the index does not list" for p in sorted(data.iterdir())
                   if p.is_dir() and p.name not in listed]
    for run in runs:
        slug = run.get("slug")
        m = _manifest(root, slug)
        if m is None:
            issues.append(f"{slug}: manifest missing or unreadable")
            continue
        mode, label = m.get("pipeline", {}).get("llm_mode"), m.get("label", "")
        faults = m.get("faults", {})
        if label.lower().startswith("offline") or mode != LIVE_MODE:
            if run.get("llm_mode") == LIVE_MODE or run.get("label") != label:
                issues.append(f"{slug}: the manifest says offline ({mode}, {label[:40]!r}) but the index does not")
        if faults.get("injected") and not run.get("faults_injected"):
            issues.append(f"{slug}: faults were injected but the index does not say so")
        if (run.get("llm_mode"), run.get("label"), bool(run.get("faults_injected")), run.get("faults_fired")) != (
                mode, label, bool(faults.get("injected")), len(faults.get("fired", []))):
            issues.append(f"{slug}: the index entry does not match the manifest (mode, label, faults)")
        try:
            source = _json(data / slug / "overlay.json").get("source", {})
        except (OSError, ValueError):
            issues.append(f"{slug}: overlay.json missing or unreadable")
            continue
        if (source.get("llm_mode"), source.get("label"), bool(source.get("faults_injected"))) != (
                mode, label, bool(faults.get("injected"))):
            issues.append(f"{slug}: overlay.json says something else than the manifest (mode, label, faults)")
    return Result("run labels", not issues, f"{len(issues)} labelling problem(s)" if issues
                  else f"index, manifests and overlays agree for all {len(runs)} runs", issues)


LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def check_links(root: Path) -> Result:
    broken, count = [], 0
    for doc in DOCS:
        path = root / doc
        if not path.exists():
            broken.append(f"{doc} is missing")
            continue
        for number, line in _without_code(_read(path)):
            for target in LINK.findall(line):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                    continue  # external (http:, mailto:) or an anchor on the same page
                count += 1
                resolved = (path.parent / target.split("#", 1)[0]).resolve()
                if not resolved.is_relative_to(root.resolve()) or not resolved.exists():
                    broken.append(f"{doc}:{number}: {target}")
    return Result("links", not broken, f"{len(broken)} broken link(s)" if broken
                  else f"all {count} relative links in {', '.join(DOCS)} resolve", broken)


def _invented_names() -> set[str]:
    names = set()
    for key, club in CLUBS.items():
        names |= {club.name, club.short_name}
        names |= {p.name for p in build_team(key, "home").players}
    return names


def check_names(root: Path) -> Result:
    issues = []
    data = root / "web" / "data"
    files = [root / "docs" / "PITCH.md", root / "web" / "index.html", *sorted((root / "web" / "js").rglob("*.js")),
             data / "index.json", *sorted(data.glob("*/manifest.json")), *sorted(data.glob("*/overlay.json"))]
    for path in (p for p in files if p.exists()):
        for number, line in enumerate(_read(path).splitlines(), 1):
            where = f"{path.relative_to(root).as_posix()}:{number}"
            if LEAGUE.search(line):
                issues.append(f"{where}: \"Premier League\"")
            issues += [f"{where}: real name {hit!r}" for hit in sorted(set(REAL_NAMES.findall(line)))]
    invented = _invented_names()
    runs, problems = _runs(root)
    issues += problems
    for run in runs:
        slug = run.get("slug")
        m = _manifest(root, slug) or {}
        shown = {run.get(side, {}).get(k) for side in ("home", "away") for k in ("name", "short_name")}
        shown |= {m.get(side, {}).get("name") for side in ("home", "away")}
        shown |= {p.get("name") for p in m.get("featured_players", [])}
        events = data / slug / "events.jsonl"
        if events.exists():
            with events.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline() or "{}")
            for side in ("home", "away"):
                team = first.get(side, {})
                shown |= {team.get("name"), team.get("short_name")} | {p.get("name") for p in team.get("players", [])}
        issues += [f"{slug}: {name!r} is not an invented club or player" for name in sorted(shown - invented - {None})]
    return Result("names", not issues, f"{len(issues)} naming problem(s)" if issues
                  else "no real league, club or player names; every published name is invented", issues)


CHECKS = (check_placeholders, check_published_runs_live, check_readme_claims, check_run_labels, check_links,
          check_names)


def run_checks(root: Path) -> list[Result]:
    return [check(Path(root)) for check in CHECKS]


def report(results: list[Result], root: Path) -> str:
    lines = [f"Pitchside submission check ({root})", ""]
    for r in results:
        lines.append(f"{'PASS' if r.ok else 'FAIL'}  {r.name:<15} {r.summary}")
        lines += [f"      - {d}" for d in r.details[:25]]
        if len(r.details) > 25:
            lines.append(f"      ... and {len(r.details) - 25} more")
    failed = sum(not r.ok for r in results)
    lines += ["", f"{failed} of {len(results)} checks failed: not ready to tag submission-v1." if failed
              else f"All {len(results)} checks passed: ready to tag submission-v1."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help="repository root to check (default: this checkout)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    results = run_checks(root)
    print(report(results, root))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
