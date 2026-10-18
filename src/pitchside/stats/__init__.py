"""Deterministic stats engine: events in, numbers and typed findings out. No LLM here."""

from .detector import DetectorConfig
from .engine import Snapshot, StatsEngine, detect
from .findings import SPELL_KINDS, Evidence, Finding, FindingKind, findings_json_schema

__all__ = [
    "SPELL_KINDS",
    "DetectorConfig",
    "Evidence",
    "Finding",
    "FindingKind",
    "Snapshot",
    "StatsEngine",
    "detect",
    "findings_json_schema",
]
