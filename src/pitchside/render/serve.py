"""``pitchside serve``: a small static file server for ``web/`` (local use only; production is Static Web Apps).

MIME types are set explicitly because the Windows registry often maps ``.js`` to ``text/plain``, which browsers
refuse for ES modules. ``--data`` serves another directory at ``/data/`` (the e2e tests use fixture bundles this way).
No directory listings, nothing outside the roots, no caching (so edits show up on reload).
"""

from __future__ import annotations

import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

MIME = {
    "": "application/octet-stream",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".ico": "image/x-icon",
}


def default_web_root() -> Path:
    for candidate in (Path.cwd() / "web", Path(__file__).resolve().parents[3] / "web"):
        if (candidate / "index.html").is_file():
            return candidate
    raise FileNotFoundError("web/index.html not found: run from the repository root or pass --root")


class Handler(SimpleHTTPRequestHandler):
    extensions_map = MIME

    def __init__(self, *args, web_root: Path, data_root: Optional[Path] = None, quiet: bool = True, **kwargs):
        self.web_root = Path(web_root).resolve()
        self.data_root = Path(data_root).resolve() if data_root else None
        self.quiet = quiet
        super().__init__(*args, directory=str(self.web_root), **kwargs)

    def translate_path(self, path: str) -> str:
        url_path = unquote(urlsplit(path).path)
        base, rel = self.web_root, url_path.lstrip("/")
        if self.data_root is not None and (url_path == "/data" or url_path.startswith("/data/")):
            base, rel = self.data_root, url_path[len("/data"):].lstrip("/")
        resolved = (base / rel).resolve()
        if resolved != base and base not in resolved.parents:
            return str(base / "__outside_root__")  # does not exist: 404
        return str(resolved)

    def list_directory(self, path):  # noqa: ARG002 - no listings
        self.send_error(404, "Not found")
        return None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature from the base class
        if not self.quiet:
            super().log_message(format, *args)


def make_server(web_root: Optional[Path] = None, data_root: Optional[Path] = None, host: str = "127.0.0.1",
                port: int = 8000, quiet: bool = True) -> ThreadingHTTPServer:
    handler = partial(Handler, web_root=web_root or default_web_root(), data_root=data_root, quiet=quiet)
    return ThreadingHTTPServer((host, port), handler)


def serve(web_root: Optional[Path] = None, data_root: Optional[Path] = None, host: str = "127.0.0.1",
          port: int = 8000, open_browser: bool = False, quiet: bool = False) -> None:
    server = make_server(web_root, data_root, host, port, quiet)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"serving {web_root or default_web_root()} at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
