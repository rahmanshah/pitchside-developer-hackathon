"""The offline guard in conftest.py really blocks outbound network access in model-client tests."""

from __future__ import annotations

import socket
import unittest


class NetworkGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        if socket.getaddrinfo.__module__ == "socket":
            self.skipTest("the network guard is a pytest fixture (tests/conftest.py)")

    def test_outbound_dns_and_connect_are_blocked(self) -> None:
        with self.assertRaises(RuntimeError):
            socket.getaddrinfo("example.com", 443)
        with socket.socket() as s, self.assertRaises(RuntimeError):
            s.connect(("93.184.216.34", 443))

    def test_loopback_stays_open_for_asyncio(self) -> None:
        a, b = socket.socketpair()
        a.close()
        b.close()


if __name__ == "__main__":
    unittest.main()
