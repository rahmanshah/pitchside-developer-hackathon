"""Narrative languages: number formatting, the words the checker needs, and template phrases.

Adding a language is one ``Language`` entry in ``LANGUAGES`` plus its code in ``PITCHSIDE_LANGUAGES``. English is
the source language (claims are written and checked in English); other languages appear in persona texts, which
keep every number as digits so the checker can verify them in any language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SOURCE_LANGUAGE = "en"


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    decimal_sep: str
    # Cardinal number words the checker treats as numbers ("one"/"uno" are left out: too often not a count).
    number_words: dict[str, int]
    # A number followed by one of these is a duration in minutes ("6 minutes"), compared against seconds.
    minute_words: tuple[str, ...]
    # Regexes whose first group is a match-clock minute ("71st minute", "minuto 71", "71'").
    clock_patterns: tuple[str, ...]
    home_words: tuple[str, ...]
    away_words: tuple[str, ...]
    # Template phrases; see ``templates.py`` for the keys and their placeholders.
    phrases: dict[str, str] = field(default_factory=dict)

    def number(self, value: float, decimals: int) -> str:
        text = f"{value:.{decimals}f}"
        return text.replace(".", self.decimal_sep) if self.decimal_sep != "." else text

    def compiled_clock_patterns(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p, re.IGNORECASE) for p in self.clock_patterns)


_EN_WORDS = {w: i for i, w in enumerate(
    "zero _ two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split()) if w != "_"}
_ES_WORDS = {w: i for i, w in enumerate(
    "cero _ dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
    "diecisiete dieciocho diecinueve veinte".split()) if w != "_"}

ENGLISH = Language(
    code="en",
    name="English",
    decimal_sep=".",
    number_words=_EN_WORDS,
    minute_words=("min", "mins", "minute", "minutes"),
    clock_patterns=(r"\b(\d{1,3})(?:st|nd|rd|th)\s+minute\b", r"\bminute\s+(\d{1,3})\b", r"\b(\d{1,3})'"),
    home_words=("the hosts", "home side", "the home team"),
    away_words=("the visitors", "away side", "the away team"),
    phrases={
        "score": "{home} {home_goals}-{away_goals} {away}",
        "goal.analyst": "Goal for {team}: a {shot_xg} xG chance from {shot_distance}. {team} have {match_shots} "
                        "shots and {match_xg} xG so far. {score}.",
        "goal.casual": "GOAL! {team} score. {score}.",
        "goal.fav_involved": "{player} scores for {team}! {score}.",
        "goal.fav_team": "{team} score, and {player}'s side make it {score}.",
        "goal.fav_other": "{team} score against {player}'s side. {score}.",
        "big_chance.analyst": "Big chance for {team}: a {shot_xg} xG shot from {shot_distance}, not converted. "
                              "{team} have {match_shots} shots and {match_xg} xG so far.",
        "big_chance.casual": "So close! {team} go near with a great chance.",
        "big_chance.fav_involved": "{player} goes close for {team} with a big chance.",
        "big_chance.fav_team": "{player}'s side {team} go close with a big chance.",
        "big_chance.fav_other": "{team} go close, but {player}'s side survive.",
        "spell.analyst": "{team} begin {label}: {facts}.",
        "spell.analyst_match": "{label_cap} begins: {facts}.",
        "spell.fav_team": "{player}'s side {team} are on top.",
        "spell.fav_other": "{player}'s side are under pressure from {team}.",
        "spell.fav_match": "{player}'s side are caught up in a frantic spell.",
        "spell_end": "{team}'s {label_end} ends after {duration}.",
        "spell_end_match": "The chaotic spell ends after {duration}.",
        "siege.casual": "{team} have {opponent} pinned in their own half.",
        "pressure_surge.casual": "{team} are pressing high and hard.",
        "momentum_shift.casual": "The game is swinging {team}'s way.",
        "chaos_spell.casual": "It's end-to-end and frantic out there.",
        "siege.label": "a siege", "siege.label_end": "siege",
        "pressure_surge.label": "a pressing surge", "pressure_surge.label_end": "pressing surge",
        "momentum_shift.label": "a spell on top", "momentum_shift.label_end": "spell on top",
        "chaos_spell.label": "a chaotic spell", "chaos_spell.label_end": "chaotic spell",
        "counter_attack": "{team} break quickly: {metres_gained} gained in {seconds_to_finish}.",
        "recap.half_time": "Half-time", "recap.full_time": "Full-time",
        "recap.analyst": "{period}: {score}. Shots: {home} {home_shots}, {away} {away_shots}. xG: {home} "
                         "{home_xg}, {away} {away_xg}. Possession: {home} {home_possession}, {away} "
                         "{away_possession}.",
        "recap.casual": "{period}: {score}.",
        "recap.goals": "Goals: {scorers}.",
        "recap.fav": "{player}: goals {goals}, shots {shots}, xG {xg}.",
        "recap.fav_quiet": "{player} has not had a shot yet.",
        "fact.field_tilt": "{v} field tilt",
        "fact.possession": "{v} possession",
        "fact.shots": "{v} shots",
        "fact.xg": "{v} xG",
        "fact.pressures_per_min": "{v} pressures per minute",
        "fact.tackles_won": "{v} tackles won",
        "fact.momentum": "momentum {v}",
        "fact.opponent_pass_completion": "{opponent} completing {v} of their passes",
        "fact.opponent_final_third_passes": "{opponent} with {v} passes into the final third",
        "fact.chaos_index": "chaos index {v}",
        "fact.turnovers_per_min": "{v} turnovers per minute",
        "fact.pass_completion": "{v} pass completion",
        "fact.mean_spell": "possessions lasting {v} on average",
        "fact.spell_duration": "lasting {v}",
        "fact.shot_xg": "a {v} xG chance",
        "fact.shot_distance": "from {v}",
        "fact.match_xg": "{v} xG in the match",
        "fact.match_shots": "{v} shots in the match",
        "fact.seconds_since_turnover": "{v} after winning the ball",
        "fact.passes_in_spell": "{v} passes in the move",
        "fact.metres_gained": "{v} gained",
        "fact.seconds_to_finish": "in {v}",
        "fact.passes_in_move": "{v} passes in the move",
        "fact.regain_distance_from_own_goal": "ball won {v} from their own goal",
        "fact.finish_distance_to_goal": "finished {v} from goal",
        "fact.ended_with_shot": "ended with a shot (1 = yes): {v}",
        "fact.possession_won_in_open_play": "ball won in open play (1 = yes): {v}",
        "fact.player_goals": "{v} goals",
        "fact.player_shots": "{v} shots",
        "fact.player_xg": "{v} xG",
        "fact.possession_total": "{v} possession",
        "fact.shots_total": "{v} shots",
        "fact.xg_total": "{v} xG",
        "fact.pass_completion_total": "{v} pass completion",
    },
)

SPANISH = Language(
    code="es",
    name="Español",
    decimal_sep=",",
    number_words=_ES_WORDS,
    minute_words=("min", "minuto", "minutos"),
    clock_patterns=(r"\bminuto\s+(\d{1,3})\b", r"\b(\d{1,3})'"),
    home_words=("los locales", "el local", "el equipo local"),
    away_words=("los visitantes", "el visitante", "el equipo visitante"),
    phrases={
        "score": "{home} {home_goals}-{away_goals} {away}",
        "goal.analyst": "Gol de {team}: una ocasión de {shot_xg} xG desde {shot_distance}. {team} suma "
                        "{match_shots} tiros y {match_xg} xG. {score}.",
        "goal.casual": "¡GOL! Marca {team}. {score}.",
        "goal.fav_involved": "¡{player} marca para {team}! {score}.",
        "goal.fav_team": "Marca {team}, el equipo de {player}. {score}.",
        "goal.fav_other": "{team} marca ante el equipo de {player}. {score}.",
        "big_chance.analyst": "Ocasión clara de {team}: un tiro de {shot_xg} xG desde {shot_distance}, sin gol. "
                              "{team} suma {match_shots} tiros y {match_xg} xG.",
        "big_chance.casual": "¡Uy! {team} roza el gol con una gran ocasión.",
        "big_chance.fav_involved": "{player} roza el gol para {team} con una ocasión clara.",
        "big_chance.fav_team": "El equipo de {player}, {team}, roza el gol.",
        "big_chance.fav_other": "{team} roza el gol, pero el equipo de {player} se salva.",
        "spell.analyst": "{team} inicia {label}: {facts}.",
        "spell.analyst_match": "Empieza {label}: {facts}.",
        "spell.fav_team": "El equipo de {player}, {team}, manda ahora.",
        "spell.fav_other": "El equipo de {player} sufre ante {team}.",
        "spell.fav_match": "El equipo de {player} vive una fase caótica.",
        "spell_end": "Termina {label_end} de {team} tras {duration}.",
        "spell_end_match": "Termina la fase caótica tras {duration}.",
        "siege.casual": "{team} encierra a {opponent} en su campo.",
        "pressure_surge.casual": "{team} aprieta arriba con mucha intensidad.",
        "momentum_shift.casual": "El partido se inclina hacia {team}.",
        "chaos_spell.casual": "Ida y vuelta: el partido se ha vuelto loco.",
        "siege.label": "un asedio", "siege.label_end": "el asedio",
        "pressure_surge.label": "una oleada de presión", "pressure_surge.label_end": "la oleada de presión",
        "momentum_shift.label": "una racha de dominio", "momentum_shift.label_end": "la racha de dominio",
        "chaos_spell.label": "una fase caótica", "chaos_spell.label_end": "la fase caótica",
        "counter_attack": "{team} sale rápido a la contra: {metres_gained} en {seconds_to_finish}.",
        "recap.half_time": "Descanso", "recap.full_time": "Final",
        "recap.analyst": "{period}: {score}. Tiros: {home} {home_shots}, {away} {away_shots}. xG: {home} "
                         "{home_xg}, {away} {away_xg}. Posesión: {home} {home_possession}, {away} "
                         "{away_possession}.",
        "recap.casual": "{period}: {score}.",
        "recap.goals": "Goles: {scorers}.",
        "recap.fav": "{player}: goles {goals}, tiros {shots}, xG {xg}.",
        "recap.fav_quiet": "{player} todavía no ha tirado a puerta.",
        "fact.field_tilt": "{v} de dominio territorial",
        "fact.possession": "{v} de posesión",
        "fact.shots": "{v} tiros",
        "fact.xg": "{v} xG",
        "fact.pressures_per_min": "{v} presiones por minuto",
        "fact.tackles_won": "{v} entradas ganadas",
        "fact.momentum": "impulso de {v}",
        "fact.opponent_pass_completion": "{opponent} completa el {v} de sus pases",
        "fact.opponent_final_third_passes": "{opponent} con {v} pases en el último tercio",
        "fact.chaos_index": "índice de caos {v}",
        "fact.turnovers_per_min": "{v} pérdidas por minuto",
        "fact.pass_completion": "{v} de pases completados",
        "fact.mean_spell": "posesiones de {v} de media",
        "fact.spell_duration": "durante {v}",
        "fact.shot_xg": "una ocasión de {v} xG",
        "fact.shot_distance": "desde {v}",
        "fact.match_xg": "{v} xG en el partido",
        "fact.match_shots": "{v} tiros en el partido",
        "fact.seconds_since_turnover": "{v} tras recuperar el balón",
        "fact.passes_in_spell": "{v} pases en la jugada",
        "fact.metres_gained": "{v} ganados",
        "fact.seconds_to_finish": "en {v}",
        "fact.passes_in_move": "{v} pases en la jugada",
        "fact.regain_distance_from_own_goal": "balón recuperado a {v} de su portería",
        "fact.finish_distance_to_goal": "remate a {v} de la portería",
        "fact.ended_with_shot": "terminó en tiro (1 = sí): {v}",
        "fact.possession_won_in_open_play": "balón recuperado en juego (1 = sí): {v}",
        "fact.player_goals": "{v} goles",
        "fact.player_shots": "{v} tiros",
        "fact.player_xg": "{v} xG",
        "fact.possession_total": "{v} de posesión",
        "fact.shots_total": "{v} tiros",
        "fact.xg_total": "{v} xG",
        "fact.pass_completion_total": "{v} de pases completados",
    },
)

LANGUAGES: dict[str, Language] = {lang.code: lang for lang in (ENGLISH, SPANISH)}


def resolve_languages(codes: tuple[str, ...]) -> tuple[Language, ...]:
    """The configured languages, English first. Fails if a code has no phrase table."""
    missing = [c for c in codes if c not in LANGUAGES]
    if missing:
        raise ValueError(f"PITCHSIDE_LANGUAGES: no phrase table for {', '.join(missing)} "
                         f"(available: {', '.join(sorted(LANGUAGES))}; see agents/languages.py)")
    ordered = [SOURCE_LANGUAGE, *(c for c in codes if c != SOURCE_LANGUAGE)]
    return tuple(LANGUAGES[c] for c in ordered)
