"""End-to-end fixtures: the real web/ served from a local thread, with a data directory built for the tests.

The data directory is a copy of web/data plus two runs that exist only here, in a temporary directory:
``live-fixture`` (a sample bundle relabelled as a Foundry run, to test the live label and deployment names) and
``unknown-fixture`` (a manifest with an unknown model mode). They are never written to web/data, so the public
demo cannot show fake output under the live label.

One-time setup: ``uv run playwright install chromium``. Run with ``uv run pytest -m e2e``.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

from pitchside.agents.bundle import LIVE_LABEL
from pitchside.render.demo_data import publish
from pitchside.render.serve import make_server

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
LIVE_DEPLOYMENT = "test-luna-deployment"


def _relabel(src: Path, dst: Path, **pipeline) -> Path:
    shutil.copytree(src, dst)
    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    manifest["pipeline"].update(pipeline)
    if pipeline.get("llm_mode") == "foundry":
        manifest["label"] = LIVE_LABEL
        manifest["run_id"] = "TEST-FIXTURE-live"
    (dst / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return dst


@pytest.fixture(scope="session")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    data = tmp / "data"
    shutil.copytree(WEB / "data", data)
    live = _relabel(data / "comeback", tmp / "live-src", llm_mode="foundry", api="responses",
                    deployments=dict.fromkeys(("interpreter", "explainer", "verifier", "personalizer"), LIVE_DEPLOYMENT))
    publish(live, data, "live-fixture", "TEST FIXTURE: relabelled as live")
    publish(data / "comeback", data, "unknown-fixture", "TEST FIXTURE: unknown mode")
    manifest = data / "unknown-fixture" / "manifest.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw["pipeline"]["llm_mode"] = "something-else"
    manifest.write_text(json.dumps(raw, indent=1), encoding="utf-8")

    server = make_server(WEB, data_root=data, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"base": f"http://127.0.0.1:{server.server_address[1]}/", "data": data}
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1600, "height": 900}, "reduced_motion": "reduce"}


@pytest.fixture
def open_page(page, site):
    """open_page("?run=comeback&t=71:30") -> the page, loaded and drawn, paused, with fonts ready."""

    def go(query: str):
        page.goto(site["base"] + query + ("&" if "?" in query else "?") + "autoplay=0&motion=0")
        page.wait_for_selector("body[data-ready='true']", timeout=15_000)
        page.evaluate("document.fonts.ready")
        return page

    return go
