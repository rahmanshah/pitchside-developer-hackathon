"""pitchside serve: module-safe MIME types, the /data override, no listings, nothing outside the roots."""

from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from pitchside.render.serve import make_server


class ServeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        web, data, cls.secret = base / "web", base / "fixtures", base / "secret.txt"
        (web / "js").mkdir(parents=True)
        (web / "data" / "run").mkdir(parents=True)
        (web / "empty").mkdir()
        (data / "run").mkdir(parents=True)
        (web / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
        (web / "js" / "app.js").write_text("export const x = 1;", encoding="utf-8")
        (web / "data" / "run" / "events.jsonl").write_text('{"web": true}\n', encoding="utf-8")
        (data / "run" / "events.jsonl").write_text('{"fixture": true}\n', encoding="utf-8")
        cls.secret.write_text("secret", encoding="utf-8")
        cls.server = make_server(web, data_root=data, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path: str):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def status(self, path: str) -> int:
        try:
            return self.get(path).status
        except urllib.error.HTTPError as error:
            return error.code

    def test_index_and_modules_have_the_right_types(self) -> None:
        with self.get("/") as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        with self.get("/js/app.js") as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"))
            self.assertEqual(r.headers["Cache-Control"], "no-store")

    def test_data_can_come_from_another_directory(self) -> None:
        with self.get("/data/run/events.jsonl") as r:
            self.assertIn(b"fixture", r.read())
            self.assertTrue(r.headers["Content-Type"].startswith("application/x-ndjson"))

    def test_no_directory_listings_and_nothing_outside_the_roots(self) -> None:
        self.assertEqual(self.status("/empty/"), 404)
        self.assertEqual(self.status("/../secret.txt"), 404)
        self.assertEqual(self.status("/%2e%2e/secret.txt"), 404)
        self.assertEqual(self.status("/data/../../secret.txt"), 404)
        self.assertEqual(self.status("/missing.js"), 404)


if __name__ == "__main__":
    unittest.main()
