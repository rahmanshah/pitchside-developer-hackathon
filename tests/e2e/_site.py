"""Helpers for the e2e tests: read the contract the page is replaying."""

from __future__ import annotations

import json


def overlay_doc(site, slug: str) -> dict:
    return json.loads((site["data"] / slug / "overlay.json").read_text(encoding="utf-8"))


def overlay(site, slug: str, overlay_id: str) -> dict:
    return next(o for o in overlay_doc(site, slug)["overlays"] if o["overlay_id"] == overlay_id)


def first(site, slug: str, kind: str) -> dict:
    return next(o for o in overlay_doc(site, slug)["overlays"] if o["kind"] == kind)


def lower_third(item: dict) -> dict:
    return next(p for p in item["placements"] if p["anchor"] == "lower_third")


def expected_lower(site, slug: str, t: float):
    hits = [o["overlay_id"] for o in overlay_doc(site, slug)["overlays"] for p in o["placements"]
            if p["anchor"] == "lower_third" and p["show_at_s"] <= t < p["hide_at_s"]]
    return hits[0] if hits else None


def state(page) -> dict:
    return page.evaluate("window.pitchside.state()")


def seek(page, t) -> dict:
    return page.evaluate("(t) => window.pitchside.seek(t)", t)
