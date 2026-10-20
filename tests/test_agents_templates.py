"""Templates are the fallback for everything, so every template output must pass the deterministic checker."""

from __future__ import annotations

import unittest

from pitchside.agents.checker import CheckContext, check_claim, check_text
from pitchside.agents.contracts import ANALYST, CASUAL, fav_persona
from pitchside.agents.jobs import FindingPoint, RecapPoint, StartPoint, walk_match
from pitchside.agents.languages import LANGUAGES, resolve_languages
from pitchside.agents.templates import template_narrative

from _agents import match

LANGS = resolve_languages(tuple(LANGUAGES))
MATCHES = [(seed, story) for story in ("comeback", "siege", "end_to_end", "balanced") for seed in (1, 2, 3)]


class TemplatesPassTheCheckerTests(unittest.TestCase):
    def test_every_template_claim_and_persona_text_passes(self) -> None:
        jobs = 0
        for seed, story in MATCHES:
            featured: list[str] = []
            for point in walk_match(match(seed, story).events):
                if isinstance(point, StartPoint):
                    featured = point.featured
                    continue
                if isinstance(point, FindingPoint):
                    job = point.job
                elif isinstance(point, RecapPoint):
                    job = point.make([])
                else:
                    continue
                jobs += 1
                out = template_narrative(job, LANGS, reason="test")
                ctx = CheckContext.for_job(job)
                with self.subTest(job=job.job_id):
                    self.assertTrue(out.claims)
                    for claim in out.claims:
                        check = check_claim(claim, ctx)
                        self.assertTrue(check.ok, f"{claim.text!r}: {check.reasons}")
                    refs = [r for c in out.claims for r in c.refs]
                    personas = {ANALYST, CASUAL, *map(fav_persona, featured)}
                    self.assertEqual({(t.persona, t.language) for t in out.texts},
                                     {(p, l.code) for p in personas for l in LANGS})
                    for text in out.texts:
                        reasons = check_text(text.text, ctx, refs, LANGUAGES[text.language])
                        self.assertEqual(reasons, [], f"{text.persona}/{text.language}: {text.text!r}")
        self.assertGreater(jobs, 150)

    def test_template_texts_are_marked_with_their_reason(self) -> None:
        point = next(p for p in walk_match(match().events) if isinstance(p, FindingPoint))
        out = template_narrative(point.job, LANGS, reason="service_unavailable: timeout")
        self.assertTrue(all(t.source == "template" and t.reason == "service_unavailable: timeout" for t in out.texts))


if __name__ == "__main__":
    unittest.main()
