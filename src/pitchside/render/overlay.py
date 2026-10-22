"""The overlay contract: timed, machine-readable overlays built from a run bundle (``overlay.json``).

Times (``show_at_s``, ``hide_at_s``) are on the **replay timeline**, not the raw match clock: the timeline is the
match clock with a half-time segment and a full-time segment added, so the half-time recap plays during the break
instead of covering the start of the second half. ``timeline.segments`` maps one to the other, and every overlay
also carries its match ``clock_s``. See ``docs/OVERLAY.md``.

Conflicts are resolved here, not in the browser, so the rules are tested once in Python and any consumer only
needs ``show_at_s <= t < hide_at_s`` per placement:

* The lower third shows one overlay at a time. A higher-priority overlay pre-empts the current one, which moves
  to the side panel. An overlay of equal or lower priority waits up to ``MAX_DELAY_S`` for the lower third to
  free up, otherwise it goes to the side panel only. Spell ends and counter-attacks (low priority context) only
  ever go to the side panel.
* The side panel lists each overlay from the moment it is not (or no longer) in the lower third, for
  ``SIDE_LINGER_S`` after it was first shown.
* A pitch highlight (shot arrow, pressure zone) is shown for the overlay's own window, when there is one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..agents.bundle import BundleManifest, NarrativeEntry, load_bundle
from ..agents.contracts import FAV_PREFIX, AuditEntry, Reason
from ..jsonl import read_events
from ..llm.usage import UsageRecord
from ..schema import PITCH_LENGTH_M, PITCH_WIDTH_M, Event
from ..stats.findings import Finding
from . import crests
from .provenance import ClaimLink, EventIndex, NumberSpan, claim_links, number_spans

OVERLAY_VERSION = "1.0"
OVERLAY_FILE = "overlay.json"
BREAK_S = 45.0
END_S = 45.0
MAX_DELAY_S = 8.0
SIDE_LINGER_S = 90.0
SIDE_ONLY_PRIORITY = 0.4
HOLD_MS = {"recap": 9000}  # real time a fast replay holds at a lower third, so it can be read (about 6 s)
DEFAULT_HOLD_MS = 6000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


class Segment(_Frozen):
    kind: Literal["play", "break", "end"] = Field(description="play: the clock runs; break/end: it is frozen.")
    period: int
    start_s: float = Field(description="Replay timeline.")
    end_s: float = Field(description="Replay timeline.")
    clock_start_s: float = Field(description="Match clock at start_s.")
    clock_end_s: float = Field(description="Match clock at end_s.")


class Timeline(_Frozen):
    duration_s: float
    segments: list[Segment]

    def play_segment(self, clock_s: float, period: Optional[int] = None) -> Segment:
        plays = [s for s in self.segments if s.kind == "play"]
        if period is not None:
            for seg in plays:
                if seg.period == period:
                    return seg
        for seg in plays:  # a clock on a period boundary belongs to the earlier period
            if clock_s <= seg.clock_end_s:
                return seg
        return plays[-1]

    def to_timeline(self, clock_s: float, period: Optional[int] = None) -> float:
        seg = self.play_segment(clock_s, period)
        clock = min(max(clock_s, seg.clock_start_s), seg.clock_end_s)
        return round(seg.start_s + clock - seg.clock_start_s, 3)

    def segment_after(self, period: int, kind: str) -> Segment:
        return next(s for s in self.segments if s.kind == kind and s.period == period)


class Rules(_Frozen):
    """The timing rules this document was resolved with (for readers; consumers need not apply them)."""

    break_s: float = BREAK_S
    end_s: float = END_S
    max_delay_s: float = MAX_DELAY_S
    side_linger_s: float = SIDE_LINGER_S
    side_only_priority: float = SIDE_ONLY_PRIORITY


class SourceInfo(_Frozen):
    llm_mode: Literal["fake", "foundry"]
    label: str = Field(description="The manifest's label, verbatim.")
    faults_injected: bool
    fault_plan: Optional[str] = None
    faults_fired: int


class TeamIdentity(_Frozen):
    team_id: str
    name: str
    short_name: str
    side: Literal["home", "away"]
    colours: crests.ClubColours
    crest: crests.Crest


class PersonaInfo(_Frozen):
    persona: str = Field(description='"analyst", "casual" or "fav:<player_id>".')
    kind: Literal["analyst", "casual", "favourite"]
    player_id: Optional[str] = None
    name: Optional[str] = None
    team_id: Optional[str] = None


Anchor = Literal["lower_third", "side_panel", "pitch_highlight"]


class Placement(_Frozen):
    anchor: Anchor
    show_at_s: float
    hide_at_s: float
    note: Optional[str] = Field(None, description="Why this window differs from the overlay's, if it does.")


class Highlight(_Frozen):
    """Pitch coordinates in metres (x 0-105, y 0-68; the home side always attacks towards x = 105)."""

    shape: Literal["arrow", "zone"]
    team_id: Optional[str] = None
    x: float
    y: float
    x2: float
    y2: float


class TextVariant(_Frozen):
    text: str
    source: Literal["model", "template"]
    reason: Optional[str] = None
    numbers: list[NumberSpan] = Field(default_factory=list, description="Verified figures in the text.")


class Provenance(_Frozen):
    source: Literal["model", "template"]
    reason: Optional[str] = None
    semantic_check: str
    claims: list[ClaimLink]


class AuditStep(_Frozen):
    seq: int
    step: str
    actor: str
    attempt: Optional[int] = None
    outcome: str
    detail: str
    next_step: Optional[str] = None
    prompt_version: Optional[str] = None


class RejectedClaim(_Frozen):
    attempt: int
    claim_id: str
    text: str
    layer: Literal["deterministic", "semantic"]
    reasons: list[Reason]


class FaultRef(_Frozen):
    role: str
    kind: str
    step: Optional[str] = None
    attempt: Optional[int] = None
    count: int = 1


class CallSummary(_Frozen):
    calls: int = Field(description="Model calls sent (refused ones are not sent).")
    errors: int
    refused: int
    transport_retries: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


Badge = Literal["template", "template_fallback", "semantic_check_skipped", "retried", "persona_template",
                "fault_injected", "transport_retry"]


class OverlayAudit(_Frozen):
    route: Literal["model", "template"]
    drafts: int
    steps: list[AuditStep]
    rejected: list[RejectedClaim]
    faults: list[FaultRef]
    calls: CallSummary
    badges: list[Badge]


class Overlay(_Frozen):
    overlay_id: str
    job_id: str
    finding_id: Optional[str]
    kind: str = Field(description="Finding kind (goal, big_chance, siege ...) or recap_half_time / recap_full_time.")
    status: Literal["moment", "start", "end", "recap"]
    team_id: Optional[str]
    player_ids: list[str] = Field(description="Players in the moment's evidence (favourite-player highlight).")
    clock_s: float = Field(description="Match clock of the moment.")
    period: int
    show_at_s: float = Field(description="Replay timeline (not match clock).")
    hide_at_s: float = Field(description="Replay timeline (not match clock).")
    priority: float = Field(description="0-1; higher wins the lower third.")
    anchor: Literal["lower_third", "side_panel"] = Field(description="Where the overlay was meant to appear.")
    placements: list[Placement] = Field(description="Resolved windows per anchor; show while show <= t < hide.")
    hold_ms: int = Field(description="Real-time hold when replay crosses its lower-third start faster than 1x.")
    highlight: Optional[Highlight] = None
    headline: str = Field(description="As written by the pipeline (English).")
    texts: dict[str, dict[str, TextVariant]] = Field(description="persona -> language -> text.")
    provenance: Provenance
    audit: OverlayAudit


class OverlayDocument(_Frozen):
    kind: Literal["pitchside-overlays"] = "pitchside-overlays"
    overlay_version: str = OVERLAY_VERSION
    match_id: str
    run_id: str
    source: SourceInfo
    teams: list[TeamIdentity]
    final_score: dict[str, int]
    languages: list[str]
    personas: list[PersonaInfo]
    timeline: Timeline
    rules: Rules = Field(default_factory=Rules)
    overlays: list[Overlay] = Field(description="Ordered by show_at_s.")


def overlay_json_schema() -> dict[str, Any]:
    schema = TypeAdapter(OverlayDocument).json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Pitchside overlays", "description": __doc__.strip().splitlines()[0], **schema}


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def build_timeline(events: Sequence[Event]) -> Timeline:
    starts = {e.period: e.clock_s for e in events if e.type == "period_start"}
    ends = {e.period: e.clock_s for e in events if e.type == "period_end"}
    segments: list[Segment] = []
    t = 0.0
    periods = sorted(starts)
    for n, period in enumerate(periods):
        clock0, clock1 = starts[period], ends.get(period, events[-1].clock_s)
        segments.append(Segment(kind="play", period=period, start_s=t, end_s=t + clock1 - clock0,
                                clock_start_s=clock0, clock_end_s=clock1))
        t += clock1 - clock0
        kind, length = ("end", END_S) if n == len(periods) - 1 else ("break", BREAK_S)
        segments.append(Segment(kind=kind, period=period, start_s=t, end_s=t + length,
                                clock_start_s=clock1, clock_end_s=clock1))
        t += length
    return Timeline(duration_s=round(t, 3), segments=segments)


def _r(x: float) -> float:
    return round(x, 3)


def _period_of(entry: NarrativeEntry, finding: Optional[Finding], index: EventIndex, timeline: Timeline) -> int:
    if finding is not None and finding.event_ids:
        event = index.by_id.get(finding.event_ids[-1])
        if event is not None:
            return event.period
    return timeline.play_segment(entry.clock_s).period


def _window(entry: NarrativeEntry, period: int, timeline: Timeline) -> tuple[float, float]:
    if entry.kind == "recap":
        seg = timeline.segment_after(period, "end" if entry.label == "full_time" else "break")
        return seg.start_s, seg.end_s
    show = timeline.to_timeline(entry.clock_s, period)
    return show, min(show + entry.display.hide_at_s - entry.display.show_at_s, timeline.duration_s)


def _highlight(entry: NarrativeEntry, finding: Optional[Finding], index: EventIndex,
               teams: dict[str, TeamIdentity]) -> Optional[Highlight]:
    if finding is None:
        return None
    triggers = [index.by_id[i] for i in finding.event_ids if i in index.by_id]
    team = finding.team_id
    home = team is not None and teams.get(team) is not None and teams[team].side == "home"
    goal_x = PITCH_LENGTH_M if home else 0.0
    shot = next((e for e in reversed(triggers) if e.type == "shot"), None)
    if finding.kind.value in ("goal", "big_chance") and shot is not None:
        return Highlight(shape="arrow", team_id=team, x=shot.location.x, y=shot.location.y, x2=goal_x,
                         y2=PITCH_WIDTH_M / 2)
    located = [e for e in triggers if getattr(e, "location", None) is not None]
    if finding.kind.value == "counter_attack" and len(located) >= 2:
        a, b = located[0].location, located[-1].location
        return Highlight(shape="arrow", team_id=team, x=a.x, y=a.y, x2=b.x, y2=b.y)
    if team is not None and finding.kind.value in ("siege", "pressure_surge", "momentum_shift"):
        depth = PITCH_LENGTH_M / 3 if finding.kind.value != "momentum_shift" else PITCH_LENGTH_M / 2
        x0 = PITCH_LENGTH_M - depth if home else 0.0
        return Highlight(shape="zone", team_id=team, x=x0, y=0.0, x2=x0 + depth, y2=PITCH_WIDTH_M)
    return None


def _texts(entry: NarrativeEntry) -> dict[str, dict[str, TextVariant]]:
    out: dict[str, dict[str, TextVariant]] = {}
    for t in entry.narrative.texts:
        out.setdefault(t.persona, {})[t.language] = TextVariant(
            text=t.text, source=t.source, reason=t.reason, numbers=number_spans(t.text, entry.narrative.claims))
    return out


def _audit(entry: NarrativeEntry, steps: list[AuditEntry], usage: list[UsageRecord],
           faults: list[dict[str, Any]]) -> OverlayAudit:
    drafts = {(d.attempt, c.claim_id): c.text for d in entry.drafts for c in d.claims}
    rejected = [RejectedClaim(attempt=c.attempt, claim_id=c.claim_id, text=drafts.get((c.attempt, c.claim_id), ""),
                              layer=c.layer, reasons=list(c.reasons))
                for c in entry.checks if not c.ok]
    grouped: dict[tuple, int] = {}
    for f in faults:
        key = (f.get("role", "?"), f.get("kind", "?"), f.get("step"), f.get("attempt"))
        grouped[key] = grouped.get(key, 0) + 1
    fault_refs = [FaultRef(role=k[0], kind=k[1], step=k[2], attempt=k[3], count=n) for k, n in grouped.items()]
    sent = [u for u in usage if u.status != "refused"]
    calls = CallSummary(calls=len(sent), errors=sum(u.status == "error" for u in usage),
                        refused=sum(u.status == "refused" for u in usage),
                        transport_retries=sum(u.attempt > 1 for u in usage),
                        input_tokens=sum(u.input_tokens for u in usage),
                        output_tokens=sum(u.output_tokens for u in usage),
                        cost_usd=round(sum(u.cost_usd for u in usage), 6))
    n = entry.narrative
    badges: list[Badge] = []
    if entry.route == "template":
        badges.append("template")
    elif n.source == "template":
        badges.append("template_fallback")
    if n.semantic_check.startswith("skipped"):
        badges.append("semantic_check_skipped")
    if len(entry.drafts) > 1:
        badges.append("retried")
    if n.source == "model" and any(t.source == "template" for t in n.texts):
        badges.append("persona_template")
    if fault_refs:
        badges.append("fault_injected")
    if calls.transport_retries:
        badges.append("transport_retry")
    return OverlayAudit(
        route=entry.route, drafts=len(entry.drafts),
        steps=[AuditStep(seq=a.seq, step=a.step, actor=a.actor, attempt=a.attempt, outcome=a.outcome,
                         detail=a.detail, next_step=a.next_step, prompt_version=a.prompt_version)
               for a in sorted(steps, key=lambda a: a.seq)],
        rejected=rejected, faults=fault_refs, calls=calls, badges=badges)


def _anchor(entry: NarrativeEntry, finding: Optional[Finding]) -> Literal["lower_third", "side_panel"]:
    if entry.kind == "recap":
        return "lower_third"
    if entry.display.priority <= SIDE_ONLY_PRIORITY or entry.label == "counter_attack":
        return "side_panel"
    if finding is not None and finding.status == "end":
        return "side_panel"
    return "lower_third"


class _Draft:
    """Mutable working copy while placements are resolved."""

    def __init__(self, key: str, show: float, hide: float, priority: float, anchor: str) -> None:
        self.key, self.show, self.hide, self.priority, self.anchor = key, show, hide, priority, anchor
        self.lower: Optional[tuple[float, float]] = None
        self.note: Optional[str] = None


MIN_LOWER_S = 1.0


def resolve_lower_third(drafts: list[_Draft]) -> None:
    """Give lower-third windows that never overlap (see the module docstring for the rules)."""
    placed: list[_Draft] = []
    for d in sorted(drafts, key=lambda d: (d.show, -d.priority, d.key)):
        if d.anchor != "lower_third":
            continue
        for p in placed:  # a delayed overlay that has not started yet yields to a more important one
            if p.lower is not None and p.lower[0] > d.show and d.priority > p.priority:
                p.lower, p.note = None, f"pre-empted by {d.key} before it was shown"
        live = [p for p in placed if p.lower is not None]
        occupant = next((p for p in live if p.lower[0] <= d.show < p.lower[1]), None)
        note = None
        if occupant is None:
            start = d.show
        elif d.priority > occupant.priority:
            if occupant.lower[0] >= d.show:
                occupant.lower, occupant.note = None, f"pre-empted by {d.key} before it was shown"
            else:
                occupant.lower, occupant.note = (occupant.lower[0], d.show), f"cut short by {d.key}"
            start = d.show
        elif occupant.lower[1] - d.show <= MAX_DELAY_S:
            start = occupant.lower[1]
            note = f"delayed {start - d.show:.1f} s for {occupant.key}"
        else:
            d.note = f"lower third busy with {occupant.key}: side panel only"
            continue
        later = [p.lower[0] for p in live if p is not occupant and p.lower is not None and p.lower[0] >= start]
        end = min([d.hide, *later])
        if end - start < MIN_LOWER_S:
            d.note = "no room in the lower third: side panel only"
            continue
        if end < d.hide:
            note = (note + "; " if note else "") + "cut short by the next overlay"
        d.lower, d.note = (start, end), note
        placed.append(d)


def build_overlay(manifest: BundleManifest, entries: Sequence[NarrativeEntry], findings: Sequence[Finding],
                  audit: Sequence[AuditEntry], usage: Sequence[UsageRecord], events: Sequence[Event]) -> OverlayDocument:
    timeline = build_timeline(events)
    index = EventIndex(events)
    by_finding = {f.finding_id: f for f in findings}
    hues = crests.club_hues(manifest.home.team_id, manifest.away.team_id)
    teams = {t.team_id: TeamIdentity(team_id=t.team_id, name=t.name, short_name=t.short_name, side=t.side,
                                     colours=crests.colours_for(t.team_id, hue),
                                     crest=crests.crest_for(t.team_id, t.short_name))
             for t, hue in zip((manifest.home, manifest.away), hues)}
    featured = {p.persona: p for p in manifest.featured_players}
    personas = [PersonaInfo(persona=p, kind="favourite", player_id=featured[p].player_id, name=featured[p].name,
                            team_id=featured[p].team_id) if p.startswith(FAV_PREFIX) and p in featured
                else PersonaInfo(persona=p, kind="analyst" if p == "analyst" else "casual")
                for p in manifest.personas]

    drafts: list[_Draft] = []
    parts: list[dict[str, Any]] = []
    for entry in entries:
        finding = by_finding.get(entry.finding_id or "")
        period = _period_of(entry, finding, index, timeline)
        show, hide = _window(entry, period, timeline)
        anchor = _anchor(entry, finding)
        draft = _Draft(entry.job_id, show, hide, entry.display.priority, anchor)
        drafts.append(draft)
        parts.append(dict(entry=entry, finding=finding, period=period, draft=draft))
    resolve_lower_third(drafts)

    overlays = []
    for part in parts:
        entry, finding, draft = part["entry"], part["finding"], part["draft"]
        placements = []
        if draft.lower is not None:
            placements.append(Placement(anchor="lower_third", show_at_s=_r(draft.lower[0]),
                                        hide_at_s=_r(draft.lower[1]), note=draft.note))
        side_from = draft.lower[1] if draft.lower is not None else draft.show
        side_to = max(draft.show + SIDE_LINGER_S, draft.hide)
        side_to = min(side_to, timeline.duration_s)
        if side_to > side_from:
            placements.append(Placement(anchor="side_panel", show_at_s=_r(side_from), hide_at_s=_r(side_to),
                                        note=draft.note if draft.lower is None else None))
        highlight = _highlight(entry, finding, index, teams)
        if highlight is not None:
            placements.append(Placement(anchor="pitch_highlight", show_at_s=_r(draft.show), hide_at_s=_r(draft.hide)))
        kind = f"recap_{entry.label}" if entry.kind == "recap" else entry.label
        status = "recap" if entry.kind == "recap" else (finding.status if finding is not None else "moment")
        hold = HOLD_MS.get("recap" if entry.kind == "recap" else entry.label, DEFAULT_HOLD_MS)
        n = entry.narrative
        overlays.append(Overlay(
            overlay_id=entry.job_id, job_id=entry.job_id, finding_id=entry.finding_id, kind=kind, status=status,
            team_id=entry.team_id, player_ids=list(dict.fromkeys(p.player_id for p in entry.ledger.players)),
            clock_s=entry.clock_s, period=part["period"], show_at_s=_r(draft.show), hide_at_s=_r(draft.hide),
            priority=entry.display.priority, anchor=draft.anchor, placements=placements,
            hold_ms=hold if draft.lower is not None else 0, highlight=highlight, headline=n.headline,
            texts=_texts(entry),
            provenance=Provenance(source=n.source, reason=n.reason, semantic_check=n.semantic_check,
                                  claims=claim_links(n.claims, entry.checks, n.semantic_check, entry.ledger, finding,
                                                     entry.clock_s, index)),
            audit=_audit(entry, [a for a in audit if a.job_id == entry.job_id],
                         [u for u in usage if u.finding_id == entry.job_id],
                         [f for f in manifest.faults.fired if f.get("job_id") == entry.job_id]),
        ))
    overlays.sort(key=lambda o: (o.show_at_s, -o.priority, o.overlay_id))
    return OverlayDocument(
        match_id=manifest.match_id, run_id=manifest.run_id,
        source=SourceInfo(llm_mode=manifest.pipeline.llm_mode, label=manifest.label,
                          faults_injected=manifest.faults.injected, fault_plan=manifest.faults.plan,
                          faults_fired=len(manifest.faults.fired)),
        teams=[teams[manifest.home.team_id], teams[manifest.away.team_id]], final_score=dict(manifest.final_score),
        languages=list(manifest.languages), personas=personas, timeline=timeline, overlays=overlays)


def overlay_from_dir(path: Path) -> OverlayDocument:
    """Build the overlay document from a bundle directory's files."""
    bundle = load_bundle(Path(path))
    events = list(read_events(Path(path) / "events.jsonl"))
    return build_overlay(bundle.manifest, bundle.narratives, bundle.findings, bundle.audit, bundle.usage, events)


def dump_overlay(doc: OverlayDocument) -> str:
    return json.dumps(doc.model_dump(mode="json"), indent=1, ensure_ascii=False) + "\n"


def write_overlay(path: Path) -> Path:
    out = Path(path) / OVERLAY_FILE
    out.write_text(dump_overlay(overlay_from_dir(path)), encoding="utf-8", newline="\n")
    return out
