"""The suite is hermetic (conftest.py): the real .env is never loaded and the deployed MCP server is never reached."""

from __future__ import annotations

import os
import socket
import unittest

import dotenv

from _hermetic import BLOCKED_ATTEMPTS, DEPLOYED_HOSTS, REAL_ENV_FILE, SENTINEL_HOST
from pitchside.llm import load_settings


class HermeticTests(unittest.TestCase):
    def test_settings_come_from_the_test_environment_not_the_real_env_file(self) -> None:
        s = load_settings()  # the path that loads .env outside the tests
        self.assertEqual((s.mode, s.tools, s.mcp_url, s.foundry_endpoint), ("fake", "local", None, None))
        self.assertNotIn("PITCHSIDE_MCP_URL", os.environ)
        self.assertNotIn("FOUNDRY_ENDPOINT", os.environ)

    def test_the_real_env_file_is_never_loaded(self) -> None:
        before = dict(os.environ)
        self.assertFalse(dotenv.load_dotenv(REAL_ENV_FILE))
        self.assertFalse(dotenv.load_dotenv())  # a path-less call would search upwards and find it
        self.assertEqual(dict(os.environ), before)

    def test_a_connection_to_the_deployed_server_fails_fast(self) -> None:
        self.assertIn(SENTINEL_HOST, DEPLOYED_HOSTS)
        for host in sorted(DEPLOYED_HOSTS):
            with self.subTest(host=host):
                with self.assertRaisesRegex(RuntimeError, "deployed MCP server"):
                    socket.getaddrinfo(host, 443)
                sock = socket.socket()
                try:
                    with self.assertRaisesRegex(RuntimeError, "deployed MCP server"):
                        sock.connect((host, 443))
                finally:
                    sock.close()
        BLOCKED_ATTEMPTS.clear()  # these attempts were the test; any other would fail the test that made it

    def test_the_configured_deployed_host_is_guarded(self) -> None:
        url = os.environ.get("PITCHSIDE_MCP_URL") or (
            dotenv.dotenv_values(REAL_ENV_FILE).get("PITCHSIDE_MCP_URL") if REAL_ENV_FILE.is_file() else None)
        if not url:
            self.skipTest("no PITCHSIDE_MCP_URL configured on this machine")
        from urllib.parse import urlparse
        self.assertIn(urlparse(url).hostname.lower(), DEPLOYED_HOSTS)


if __name__ == "__main__":
    unittest.main()
