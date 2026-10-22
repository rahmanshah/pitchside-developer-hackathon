# The overlay contract (`overlay.json`)

`overlay.json` is the timed, machine-readable list of on-screen overlays for one recorded match run. It is derived
from the other files of a run bundle (`pitchside.render.overlay`), written into the bundle by `pitchside run`, and
rebuilt with `uv run pitchside overlay <bundle-dir>`. The web UI (`web/`) replays it; any other renderer can too.

- JSON Schema: [`overlay.schema.json`](overlay.schema.json) (`uv run pitchside schema --overlay`; CI fails if stale)
- Version: `overlay_version` "1.0"; the bundle format is 1.1 since this file was added

## Times: the replay timeline, not the match clock

**`show_at_s` and `hide_at_s` are seconds on the replay timeline, not the raw match clock.** The bundle's match clock
runs from 0 to 5400 s with half-time at exactly 2700 s and no break, so a half-time recap timed on the clock would
cover the start of the second half. The replay timeline adds two frozen-clock segments:

| Segment | Timeline (s) | Match clock (s) |
|---|---|---|
| `play`, period 1 | 0 - 2700 | 0 - 2700 |
| `break` (half-time) | 2700 - 2745 | 2700 (frozen) |
| `play`, period 2 | 2745 - 5445 | 2700 - 5400 |
| `end` (full-time) | 5445 - 5490 | 5400 (frozen) |

`timeline.segments` gives this mapping for each document (the lengths are in `rules`). A match-clock moment `c` in
period `p` sits at `segment.start_s + c - segment.clock_start_s` of that period's `play` segment. Every overlay also
carries its match `clock_s` and `period`, for display. The half-time recap fills the `break` segment and the
full-time recap the `end` segment. Events (`events.jsonl`) and stats snapshots keep match-clock times; a renderer
maps them with the same segments (a clock on a period boundary belongs to the earlier period unless the event says
otherwise).

## One overlay

| Field | Meaning |
|---|---|
| `overlay_id`, `job_id`, `finding_id` | Links to the bundle's narratives, audit, usage and findings |
| `kind` | `goal`, `big_chance`, `momentum_shift`, `pressure_surge`, `siege`, `chaos_spell`, `counter_attack`, `recap_half_time`, `recap_full_time` |
| `status` | `moment`, `start` or `end` (spells), `recap` |
| `team_id`, `player_ids` | Who the moment is about; players in its evidence (used by the favourite-player view) |
| `clock_s`, `period` | Match clock of the moment |
| `show_at_s`, `hide_at_s` | The overlay's own window on the replay timeline |
| `priority` | 0-1 from the bundle (goals and recaps 1.0, spell ends at most 0.4) |
| `anchor` | Where it was meant to go: `lower_third` or `side_panel` |
| `placements` | The resolved windows, one per anchor: `lower_third`, `side_panel`, `pitch_highlight`. **Show a placement while `show_at_s <= t < hide_at_s`**; `note` says why a window differs from the overlay's own |
| `hold_ms` | When a replay runs faster than real time and crosses the lower-third start, hold the clock this long (real time) so the text can be read |
| `highlight` | Pitch geometry in metres: an `arrow` (shot to goal, counter-attack run) or a `zone` (pressing or siege third, momentum half). The home side always attacks towards x = 105 (a known simplification of the generator) |
| `headline` | As written by the pipeline (English) |
| `texts` | `persona -> language -> {text, source, reason, numbers}`; `numbers` locates each verified figure in the text (`start`, `end`, ledger `refs`) |
| `provenance` | Narrative source, template reason, `semantic_check`, and every shown claim with its evidence chain (below) |
| `audit` | Condensed audit trail: steps, rejected claims with reasons, injected faults, model-call summary, badges |

### Placement rules (resolved when the file is built)

Consumers do not need to resolve conflicts; the rules are applied once, in Python, and tested:

1. The lower third shows one overlay at a time.
2. A higher-priority overlay pre-empts the current one, which is cut short and moves to the side panel.
3. An overlay of equal or lower priority waits up to `max_delay_s` (8 s) for the lower third; otherwise it is shown
   in the side panel only. A waiting overlay that has not started yet yields to a more important one.
4. Spell ends and counter-attacks (priority at most `side_only_priority`, 0.4, or low-severity context) only go to
   the side panel.
5. The side panel lists an overlay from when it is not, or no longer, in the lower third until `side_linger_s` (90 s)
   after its own start.
6. A pitch highlight is shown for the overlay's own window.

### Evidence chain (`provenance.claims[].evidence[]`)

Each shown claim (in English, as the checker verified it) lists its ledger refs, resolved to the metric, team or
player, value, unit, the phrase it is quoted as, its time window (match clock), baseline, and `events`:

- `triggering`: the events the stats engine recorded as triggering the finding (`Finding.event_ids`), on the finding's
  own evidence (`e*`).
- `in_window`: events the metric is computed from, selected by a deterministic `filter` (event types for the metric,
  team or player, time window) stored next to the ids. Context numbers (`c*`), recap numbers (`source: claim`, with
  the `origin` job of the earlier verified claim) and findings without event ids only have this relation.
- `player`: a player's events at the moment (`p*` refs: scorer's shot, ball winner's tackle).

At most 200 ids are listed per link; `count` gives the full number. The UI labels triggering and in-window events
differently: "events in the window" are not "the events that caused this".

### Audit badges

| Badge | When |
|---|---|
| `template` | The job never went to the agents (spell ends, low-severity findings) |
| `template_fallback` | The agents were asked but a template narrative was shown (`provenance.reason` says why) |
| `semantic_check_skipped` | The semantic verifier was unavailable; claims were accepted on the deterministic check alone |
| `retried` | The explainer wrote more than one draft |
| `persona_template` | At least one persona text is a template (personalizer failure or a failed persona check) |
| `fault_injected` | A fault was injected on purpose into this job (`audit.faults`) |
| `transport_retry` | A model call was retried at the transport level (rate limit, timeout, server error) |

## Document level

- `source`: `llm_mode` (`fake` or `foundry`), the manifest `label` verbatim, `faults_injected`, `fault_plan`,
  `faults_fired`. Renderers must label runs honestly from these (see the README).
- `teams`: generated colours (`primary`, `secondary`, `text`, on-pitch `accent`) and a crest pattern; every club is
  invented and so are its colours (`pitchside.render.crests`, contrast-checked).
- `personas`: `analyst`, `casual`, and `fav:<player_id>` for each featured player, with name and club.
- `languages`, `final_score`, `timeline`, `rules`.
