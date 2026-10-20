"""Offline agents: deterministic responders that read the real prompt and answer like a careful model.

They parse the same prompt blocks a live model gets (``prompting.py``), so an offline run exercises the prompt
builders, the tool loop, the checker, the verifier loop and the personalizer end to end. They never invent
numbers: every claim is assembled from the evidence lines' "say" phrases. Offline translations are not real
translations; they are tagged ``[es · offline]`` so nobody mistakes them for model output.

Offline numbers are **plumbing checks, not model quality**: these agents are correct by construction, so the
interesting offline metrics are the ones with deliberately injected faults (``faults.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Union

from ..llm.config import Role
from ..llm.fake import ToolCall
from .checker import scan

_ITEM = re.compile(r"^(?:\w+: )?([ec]\d+) \| (\w+) \| (\S+) \| (-?[\d.e+-]+) (\w+)(?: \| [\d:]+-[\d:]+)? \| say \"(.*)\"$")
_PLAYER = re.compile(r"^(?:\w+: )?(p\d+) \| (.+?) \| (\w+) \| (\w+)$")
_TOOL_REF = re.compile(r'"ref":\s*"(c\d+)"')
OVERCLAIM_WORDS = ("certainly", "surely", "will win", "guaranteed", "best ever", "always", "never")
TOOL_LABELS = ("goal", "half_time", "full_time")


@dataclass(frozen=True)
class _Item:
    ref: str
    metric: str
    team: Optional[str]
    say: str


@dataclass(frozen=True)
class _Player:
    ref: str
    name: str
    team: str
    involvement: str


def _lines(transcript: str) -> list[str]:
    return [line.strip() for line in transcript.splitlines()]


def _field(transcript: str, name: str) -> Optional[str]:
    for line in _lines(transcript):
        line = re.sub(r"^\w+: (?=[A-Z]+[A-Z ()a-z]*:)", "", line)
        if line.startswith(f"{name}:"):
            return line[len(name) + 1:].strip()
    return None


def _block(transcript: str, title: str) -> list[str]:
    out, inside = [], False
    for line in _lines(transcript):
        bare = re.sub(r"^user: ", "", line)
        if bare.startswith(title):
            inside = True
            continue
        if inside:
            if re.match(r"^[A-Z][A-Z ()]+(?: \(attempt \d+\))?:", bare) or not bare or bare.startswith("tool:"):
                break
            out.append(bare)
    return out


def _items(transcript: str) -> list[_Item]:
    found = []
    for line in _lines(transcript):
        m = _ITEM.match(line)
        if m:
            found.append(_Item(m.group(1), m.group(2), None if m.group(3) == "match" else m.group(3), m.group(6)))
    return found


def _players(transcript: str) -> list[_Player]:
    found = []
    for line in _lines(transcript):
        m = _PLAYER.match(line)
        if m:
            found.append(_Player(*m.groups()))
    return found


def _clubs(transcript: str) -> dict[str, str]:
    text = _field(transcript, "CLUBS") or ""
    return {m.group(1): m.group(2) for m in re.finditer(r"(\w+) = (.+?) \((?:home|away)\)", text)}


def _label(transcript: str) -> str:
    moment = _field(transcript, "MOMENT") or ""
    m = re.match(r"(?:recap )?(\w+)", moment)
    return m.group(1) if m else "moment"


def _moment_team(transcript: str) -> Optional[str]:
    m = re.search(r"\| team (\w+) \(", _field(transcript, "MOMENT") or "")
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Responders
# --------------------------------------------------------------------------- #


def interpreter(transcript: str) -> Union[str, Sequence[ToolCall]]:
    label = _label(transcript)
    tool_refs = _TOOL_REF.findall(transcript)
    if "TOOLS:" in transcript and "tool:" not in transcript and label in TOOL_LABELS:
        return [ToolCall("match_totals")]
    items, players = _items(transcript), _players(transcript)
    if label in ("half_time", "full_time"):
        numbers = [i.ref for i in items if i.metric.endswith("_total")][:2]
        numbers += [i.ref for i in items if i.ref not in numbers and not i.metric.endswith("_total")][:2]
    else:
        numbers = [i.ref for i in items if i.ref.startswith("e")][:3] + tool_refs[:1]
    focus = [p.ref for p in players[:1]] + numbers
    return json.dumps({"angle": f"A {label.replace('_', ' ')} worth explaining.", "focus_refs": focus,
                       "note": "Offline interpreter: evidence picked by rule."})


def _numbers_in(say: str, ref: str) -> list[dict]:
    return [{"value": t.value, "ref": ref} for t in scan(say).numbers]


def explainer(transcript: str) -> str:
    items = {i.ref: i for i in _items(transcript)}
    players = {p.ref: p for p in _players(transcript)}
    clubs = _clubs(transcript)
    brief = _field(transcript, "BRIEF") or ""
    m = re.search(r"\| focus ([^|]+)", brief)
    focus = [r.strip() for r in (m.group(1).split(",") if m else []) if r.strip() in items or r.strip() in players]
    if not any(r in items for r in focus):
        focus += [r for r in items if r.startswith("e")][:3] or list(items)[:3]
    label = _label(transcript)
    claims = []
    for ref in [r for r in focus if r in players][:1]:
        p = players[ref]
        verb = {"scorer": "scored for", "shooter": "had the chance for", "ball_winner": "won the ball for"}.get(
            p.involvement, "featured for")
        claims.append({"text": f"{p.name} {verb} {clubs.get(p.team, p.team)}.", "team_id": p.team, "refs": [ref],
                       "numbers": []})
    numbered = [items[r] for r in focus if r in items]
    by_team: dict[Optional[str], list[_Item]] = {}
    for item in numbered:
        by_team.setdefault(item.team, []).append(item)
    for team, group in by_team.items():
        for start in range(0, len(group), 2):
            chunk = group[start:start + 2]
            parts = []
            for item in chunk:
                named = any(name in item.say for name in clubs.values()) or ":" in item.say
                prefix = "" if named or item.team is None or parts else f"{clubs.get(item.team, item.team)}: "
                parts.append(prefix + item.say)
            text = ", ".join(parts)
            text = (text[:1].upper() + text[1:]) + "."
            numbers = [n for item in chunk for n in _numbers_in(item.say, item.ref)]
            claims.append({"text": text, "team_id": team, "refs": [i.ref for i in chunk], "numbers": numbers})
    team = _moment_team(transcript)
    headline = f"{label.replace('_', ' ').capitalize()}: {clubs.get(team, 'both clubs') if team else 'the match'}"
    return json.dumps({"headline": headline, "claims": claims[:3]})


def verifier(transcript: str) -> str:
    verdicts = []
    for line in _block(transcript, "CLAIMS"):
        m = re.match(r"(\d+)\. \[[^\]]*\] (.*)$", line)
        if not m:
            continue
        text = m.group(2).lower()
        bad = next((w for w in OVERCLAIM_WORDS if w in text), None)
        verdicts.append({"claim": int(m.group(1)), "supported": bad is None,
                         "reason": "ok" if bad is None else f"Overclaims ('{bad}'); the evidence does not show that."})
    return json.dumps({"verdicts": verdicts})


def personalizer(transcript: str) -> str:
    claims = [re.sub(r"^\d+\. ", "", line) for line in _block(transcript, "CLAIMS (verified)")]
    personas_text = _field(transcript, "PERSONAS") or "analyst | casual"
    personas = [(m.group(1), m.group(2)) for m in re.finditer(r"([\w:]+)(?: \(([^,]+),[^)]*\))?(?: \||$)",
                                                                  personas_text)]
    languages = [l.strip() for l in (_field(transcript, "LANGUAGES") or "en").split(",")]
    texts = []
    for code in languages:
        tag = "" if code == "en" else f"[{code} · offline] "
        for persona, name in personas:
            if persona == "analyst":
                body = " ".join(claims[:2])
            elif persona == "casual":
                body = claims[0] if claims else ""
            else:
                body = f"For {name}'s fans: {claims[0]}" if claims else ""
            texts.append({"persona": persona, "language": code, "text": tag + body})
    return json.dumps({"texts": texts})


RESPONDERS: dict[Role, Callable[[str], Union[str, Sequence[ToolCall]]]] = {
    Role.INTERPRETER: interpreter,
    Role.EXPLAINER: explainer,
    Role.VERIFIER: verifier,
    Role.PERSONALIZER: personalizer,
}
