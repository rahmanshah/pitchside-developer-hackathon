"""End-to-end fixtures: the real web/ served from a local thread, with a data directory built for the tests.

The smoke tests never read web/data, so publishing live bundles there cannot break them. Their data directory is
built in a temporary directory from the offline sample definitions, run fresh on the fake model client under
fixture-only slugs (see ``_site``), plus two derived runs: ``live-fixture`` (the comeback fixture relabelled as a
Foundry run, to test the live label and deployment names) and ``unknown-fixture`` (a manifest with an unknown model
mode). None of these is ever written to web/data, so the public demo cannot show fake output under the live label.

``published_site`` serves web/data as it is, for the one test that checks whatever is published (whatever its mode).

One-time setup: ``uv run playwright install chromium``. Run with ``uv run pytest -m e2e``.
"""

from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pitchside.agents.bundle import LIVE_LABEL
from pitchside.render.demo_data import SAMPLES, publish, run_sample
from pitchside.render.serve import make_server

from _site import COMEBACK, FAULTS, LIVE_RUN, UNKNOWN_RUN

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
LIVE_DEPLOYMENT = "test-luna-deployment"
FIXTURES = {COMEBACK: "comeback", FAULTS: "faults-demo"}  # fixture slug -> the sample it runs


def _relabel(src: Path, dst: Path, **pipeline) -> Path:
    shutil.copytree(src, dst)
    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    manifest["pipeline"].update(pipeline)
    if pipeline.get("llm_mode") == "foundry":
        manifest["label"] = LIVE_LABEL
        manifest["run_id"] = "TEST-FIXTURE-live"
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return dst


def _serve(data: Path | None):
    server = make_server(WEB, data_root=data, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def _opener(page, base: str):
    def go(query: str):
        page.goto(base + query + ("&" if "?" in query else "?") + "autoplay=0&motion=0")
        page.wait_for_selector("body[data-ready='true']", timeout=15_000)
        page.evaluate("document.fonts.ready")
        return page

    return go


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    data = tmp / "data"
    samples = {s.slug: s for s in SAMPLES}
    for slug, sample in FIXTURES.items():
        # pytest-playwright's sync API keeps an event loop running on this thread, and the pipeline uses asyncio.run
        with ThreadPoolExecutor(max_workers=1) as pool:
            bundle = pool.submit(run_sample, samples[sample], tmp / "bundles").result()
        publish(bundle, data, slug, f"TEST FIXTURE: {samples[sample].title}", samples[sample].description)
    live = _relabel(data / COMEBACK, tmp / "live-src", llm_mode="foundry", api="responses",
                    deployments=dict.fromkeys(("interpreter", "explainer", "verifier", "personalizer"), LIVE_DEPLOYMENT))
    publish(live, data, LIVE_RUN, "TEST FIXTURE: relabelled as live")
    publish(data / COMEBACK, data, UNKNOWN_RUN, "TEST FIXTURE: unknown mode")
    manifest = data / UNKNOWN_RUN / "manifest.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["pipeline"]["llm_mode"] = "something-else"
    manifest.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    server, base = _serve(data)
    yield {"base": base, "data": data}
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def published_site():
    server, base = _serve(None)  # web/data, unchanged
    yield {"base": base, "data": WEB / "data"}
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1600, "height": 900}, "reduced_motion": "reduce"}


@pytest.fixture
def open_page(page, site):
    """open_page(f"?run={COMEBACK}&t=71:30") -> the page, loaded and drawn, paused, with fonts ready."""
    return _opener(page, site["base"])


@pytest.fixture
def open_published(page, published_site):
    """Like open_page, on the runs published in web/data."""
    return _opener(page, published_site["base"])
