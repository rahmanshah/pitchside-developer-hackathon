"""The MCP server as deployed on Container Apps: match specs with clubs, /healthz, the request rate limit."""

from __future__ import annotations

import asyncio
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import httpx
import uvicorn

from pitchside.agents.pipeline import RunConfig, build_pipeline, run_match
from pitchside.generator import generate_match
from pitchside.llm import load_settings
from pitchside.mcp_server import build_server, http_app, load_store, parse_generate
from pitchside.ratelimit import DEFAULT_BURST, DEFAULT_PER_MINUTE, Bucket, RateLimit

HOST = "pitchside-mcp.example.azurecontainerapps.io"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
_TMP = tempfile.TemporaryDirectory()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SpecTests(unittest.TestCase):
    def test_specs_name_seed_storyline_and_clubs(self) -> None:
        self.assertEqual(tuple(parse_generate("3")), (3, "comeback", "aur", "irv"))
        self.assertEqual(tuple(parse_generate("4:siege")), (4, "siege", "aur", "irv"))
        self.assertEqual(tuple(parse_generate("3:siege:har:wst")), (3, "siege", "har", "wst"))
        for bad in ("x", "3:nonsense", "3:siege:har", "3:siege:har:har", "3:siege:har:zzz", "3:siege:har:wst:x"):
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                parse_generate(bad)

    def test_specs_with_clubs_load_the_published_sample_matches(self) -> None:
        store = load_store(generate=["7:comeback:aur:irv", "3:siege:har:wst", "4:end_to_end:wst:aur"])
        self.assertEqual(store.ids(), ["aur_irv_comeback_0007", "har_wst_siege_0003", "wst_aur_end_to_end_0004"])


class BucketTests(unittest.TestCase):
    def test_a_bucket_allows_its_burst_then_refills_at_its_rate(self) -> None:
        b = Bucket(per_minute=60, capacity=3)
        self.assertEqual([b.take(0.0) for _ in range(3)], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(b.take(0.0), 1.0)  # one token per second at 60/min
        self.assertEqual(b.take(1.0), 0.0)
        self.assertAlmostEqual(b.take(1.5), 0.5)
        self.assertEqual(b.take(100.0), 0.0)  # refills to capacity, not beyond
        self.assertEqual([b.take(100.0) for _ in range(2)], [0.0, 0.0])
        self.assertGreater(b.take(100.0), 0.0)


async def _ok(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


def _get(app, path="/mcp", headers=None, client=("10.0.0.9", 5000)):
    async def go():
        transport = httpx.ASGITransport(app=app, client=client)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.get(path, headers=headers or {})
    return asyncio.run(go())


class RateLimitTests(unittest.TestCase):
    def test_a_client_over_its_burst_gets_429_with_retry_after(self) -> None:
        clock = FakeClock()
        app = RateLimit(_ok, per_minute=60, burst=2, clock=clock)
        self.assertEqual([_get(app).status_code for _ in range(2)], [200, 200])
        limited = _get(app)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "1")
        self.assertEqual(limited.json()["error"], "rate_limited")
        clock.now += 1.0
        self.assertEqual(_get(app).status_code, 200)
        self.assertEqual(app.limited, 1)

    def test_the_health_probe_is_never_limited(self) -> None:
        app = RateLimit(_ok, per_minute=60, burst=1, clock=FakeClock())
        self.assertEqual([_get(app, "/healthz").status_code for _ in range(5)], [200] * 5)
        self.assertEqual(_get(app).status_code, 200)

    def test_forwarded_clients_are_separate_only_behind_a_trusted_proxy(self) -> None:
        trusted = RateLimit(_ok, per_minute=60, burst=1, trust_forwarded=True, clock=FakeClock())
        a = {"x-forwarded-for": "198.51.100.1, 203.0.113.7"}
        b = {"x-forwarded-for": "203.0.113.8"}
        self.assertEqual(_get(trusted, headers=a).status_code, 200)
        self.assertEqual(_get(trusted, headers=b).status_code, 200)  # another client: its own bucket
        self.assertEqual(_get(trusted, headers={"x-forwarded-for": "1.2.3.4, 203.0.113.7"}).status_code, 429)
        untrusted = RateLimit(_ok, per_minute=60, burst=1, clock=FakeClock())
        self.assertEqual(_get(untrusted, headers=a).status_code, 200)
        self.assertEqual(_get(untrusted, headers=b).status_code, 429)  # a spoofed header changes nothing

    def test_the_global_bucket_caps_all_clients_together(self) -> None:
        app = RateLimit(_ok, per_minute=600, burst=100, global_per_minute=2, clock=FakeClock())
        app.global_bucket.capacity = 2
        codes = [_get(app, client=(f"10.0.0.{i}", 1)).status_code for i in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_client_memory_is_bounded(self) -> None:
        app = RateLimit(_ok, max_clients=3, clock=FakeClock())
        for i in range(10):
            _get(app, client=(f"10.0.1.{i}", 1))
        self.assertLessEqual(len(app.buckets), 3)

    def test_bad_limits_are_refused(self) -> None:
        for kwargs in ({"per_minute": 0}, {"burst": 0}, {"global_per_minute": -1}):
            with self.assertRaises(ValueError):
                RateLimit(_ok, **kwargs)

    def test_lifespan_passes_through_so_the_mcp_session_manager_starts(self) -> None:
        events = []

        async def inner(scope, receive, send):
            assert scope["type"] == "lifespan"
            events.append((await receive())["type"])
            await send({"type": "lifespan.startup.complete"})

        async def go():
            messages = iter([{"type": "lifespan.startup"}])
            sent = []

            async def receive():
                return next(messages)

            async def send(message):
                sent.append(message["type"])

            await RateLimit(inner)({"type": "lifespan"}, receive, send)
            return sent
        self.assertEqual(asyncio.run(go()), ["lifespan.startup.complete"])
        self.assertEqual(events, ["lifespan.startup"])


class DeployedServerTests(unittest.TestCase):
    """The real MCP app behind the real rate limit, as `pitchside mcp --transport streamable-http` serves it."""

    def server(self):
        return build_server(load_store(generate=["7:comeback:aur:irv"]), stateless_http=True, json_response=True,
                            allowed_hosts=[HOST])

    def test_healthz_answers_without_a_host_check(self) -> None:
        server = self.server()
        response = _get(http_app(server, {}), "/healthz", headers={"host": "100.100.0.12:8000"})
        self.assertEqual((response.status_code, response.json()["matches"]), (200, 1))

    def run_match(self, seed, storyline, home, away, limits):
        server = build_server(load_store(generate=[f"{seed}:{storyline}:{home}:{away}"]), stateless_http=True,
                              json_response=True, allowed_hosts=[HOST])
        app = http_app(server, limits)
        result = generate_match(seed=seed, storyline=storyline, home=home, away=away)
        s = load_settings({"PITCHSIDE_USAGE_LOG": str(Path(tempfile.mkdtemp(dir=_TMP.name)) / "u.jsonl"),
                           "PITCHSIDE_TOOLS": "mcp", "PITCHSIDE_MCP_URL": f"http://{HOST}/mcp"}, env_file=None)

        async def go():
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}")
            pipeline = build_pipeline(s, mcp_http_client=client)
            try:
                async with server.session_manager.run():
                    return await run_match(result.events, pipeline, RunConfig.from_settings(s), run_id="deployed",
                                           answer_key=result.answer_key)
            finally:
                await client.aclose()
                await pipeline.aclose()
        return asyncio.run(go()), app

    def test_a_whole_match_stays_well_inside_the_default_limit(self) -> None:
        run, app = self.run_match(7, "comeback", "aur", "irv", {})
        self.assertEqual(app.limited, 0)
        self.assertEqual(len(app.buckets), 1)  # one client
        used = DEFAULT_BURST - app.buckets[next(iter(app.buckets))].tokens
        self.assertLess(used, DEFAULT_BURST / 3)  # about 10 requests per match offline: room for a live model
        self.assertGreaterEqual(DEFAULT_PER_MINUTE, 60)
        self.assertTrue(all(r.narrative for r in run.records))

    def test_a_rate_limited_run_still_finishes_with_every_job_narrated(self) -> None:
        run, app = self.run_match(7, "comeback", "aur", "irv",
                                  {"per_minute": 0.001, "burst": 4, "global_per_minute": 1000})
        self.assertGreater(app.limited, 0)
        self.assertTrue(run.records)
        self.assertTrue(all(r.narrative.texts for r in run.records))


class RealSocketTests(unittest.TestCase):
    def test_uvicorn_serves_the_rate_limited_app_over_a_real_socket(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = build_server(load_store(generate=["7:comeback"]), stateless_http=True, json_response=True,
                              allowed_hosts=["127.0.0.1"])
        config = uvicorn.Config(http_app(server, {"per_minute": 1, "burst": 2}), host="127.0.0.1", port=port,
                                log_level="warning", access_log=False)
        uv = uvicorn.Server(config)
        thread = threading.Thread(target=uv.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 10
            while not uv.started and time.time() < deadline:
                time.sleep(0.05)
            base = f"http://127.0.0.1:{port}"
            self.assertEqual(httpx.get(f"{base}/healthz").status_code, 200)
            codes = [httpx.post(f"{base}/mcp", json=LIST_TOOLS, headers=MCP_HEADERS).status_code for _ in range(3)]
            self.assertEqual(codes, [200, 200, 429])
        finally:
            uv.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
