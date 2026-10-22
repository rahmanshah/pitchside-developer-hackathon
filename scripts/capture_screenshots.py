"""Capture the README screenshots into docs/img/: fixed bundles, fixed times, 1920 x 1080, no motion, no autoplay.

    uv run python scripts/capture_screenshots.py            # needs `uv run playwright install chromium` once

Everything shown comes from the offline sample runs in web/data (invented clubs, fake model client), so the images
carry no account names, keys or real-world marks; the page is captured without browser chrome or URL bar. Re-run it
after regenerating web/data (or after the live bundles replace the samples) to refresh the images.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pitchside.render.serve import make_server  # noqa: E402

VIEWPORT = {"width": 1920, "height": 1080}
SHOTS = [
    ("broadcast.png", "Broadcast view, clean mode: 72' goal lower third",
     "?run=comeback&t=71:30&clean=1&persona=casual&lang=en"),
    ("analyst.png", "Analyst dashboard at the same moment, analyst persona",
     "?run=comeback&view=analyst&t=71:35&persona=analyst&lang=en"),
    ("evidence-drawer.png", "Evidence drawer: a moment whose semantic check was skipped (injected verifier timeout)",
     "?run=faults-demo&t=34:20&persona=analyst&lang=en&drawer=wst_aur_end_to_end_0004:big_chance:1"),
]


def capture(out: Path, data: Path | None) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    server = make_server(ROOT / "web", data_root=data, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    written = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1, reduced_motion="reduce")
            for name, caption, query in SHOTS:
                page.goto(f"{base}{query}&autoplay=0&motion=0")
                page.wait_for_selector("body[data-ready='true']", timeout=15_000)
                page.evaluate("document.fonts.ready")
                page.wait_for_timeout(300)
                path = out / name
                page.screenshot(path=str(path))
                written.append(path)
                print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}  {path.stat().st_size // 1024} KB  {caption}")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "docs" / "img"))
    parser.add_argument("--data", help="serve this data directory instead of web/data")
    args = parser.parse_args()
    capture(Path(args.out), Path(args.data) if args.data else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
