"""Web app tests: a public URL cannot be talked into anything. [Track A, Sprint 7]

The site is the one part of this project we point a public ngrok URL at, so
these tests are mostly about what a request CANNOT do:

  * no request reaches a file the server did not intend to serve, however the
    path is spelled;
  * no request composes a change outside the base design's own vocabulary, or
    a number outside a stated range;
  * no request lowers the safety-factor floor -- and the refusal it gets is
    `gate.resolve_threshold`'s, not a message this layer invented;
  * no request hands us a Decision of its own: `tamper` acts on the Decision
    the named run really produced.

Plus the two things the demo depends on being true: every change kind runs end
to end, and the enforcement button really raises.

The HTTP tests run a real `ThreadingHTTPServer` on an ephemeral port. Nothing
here touches the network, a model, or the filesystem.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import MEMBER_IDS  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.web import api  # noqa: E402
from earl.web import app as web_app  # noqa: E402
from earl.web.app import MAX_BODY_BYTES, RateLimiter, serve  # noqa: E402


# ==========================================================================
# The domain layer
# ==========================================================================

class TestBaseModel(unittest.TestCase):
    def test_describes_the_whole_truss(self):
        model = api.base_model()
        self.assertEqual([m["id"] for m in model["members"]], MEMBER_IDS)
        self.assertEqual(len(model["nodes"]), 6)
        self.assertEqual(len(model["scenarios"]), 20)

    def test_capacity_convention_is_stated_not_assumed(self):
        """The 25 ksi allowable vs the 50 ksi yield is the single convention
        most likely to be misread by someone reading the page, so it is sent
        to the browser rather than left in a docstring."""
        material = api.base_model()["material"]
        self.assertEqual(material["allowable_ksi"], 25.0)
        self.assertEqual(material["yield_ksi"], 50.0)
        self.assertIn("allowable", material["note"])

    def test_two_supported_nodes(self):
        nodes = {n["id"]: n["support"] for n in api.base_model()["nodes"]}
        self.assertEqual(sorted(n for n, s in nodes.items() if s == "pin"), ["n5", "n6"])


class TestRequestValidation(unittest.TestCase):
    """Everything a request can say, and everything it cannot."""

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(api.BadRequest):
            api.build_scenario({"kind": "exec"})

    def test_member_id_must_be_a_member(self):
        for bogus in ["../../etc/passwd", "m11", "", None, 7, "m1; rm -rf /"]:
            with self.assertRaises(api.BadRequest):
                api.build_scenario({"kind": "resize", "member_id": bogus, "area_in2": 1.0})

    def test_node_id_must_be_a_node(self):
        with self.assertRaises(api.BadRequest):
            api.build_scenario(
                {"kind": "add_load", "node_id": "n99", "down_kips": 1, "east_kips": 0}
            )

    def test_areas_are_clamped_to_a_stated_range(self):
        for area in [0.0, -5.0, 1e9, api.AREA_MAX_IN2 + 0.001]:
            with self.assertRaises(api.BadRequest):
                api.build_scenario({"kind": "resize", "member_id": "m1", "area_in2": area})

    def test_non_finite_numbers_are_refused(self):
        """float('nan') survives json.loads with the default parser, and a NaN
        area would reach the solver. It must not."""
        for value in ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")]:
            with self.assertRaises(api.BadRequest):
                api.build_scenario({"kind": "resize", "member_id": "m1", "area_in2": value})

    def test_a_load_must_move_somewhere_a_load_exists(self):
        with self.assertRaises(api.BadRequest):
            api.build_scenario({"kind": "move_load", "from_node": "n1", "to_node": "n3"})

    def test_a_change_that_changes_nothing_is_refused(self):
        with self.assertRaises(api.BadRequest):
            api.build_scenario({"kind": "move_load", "from_node": "n2", "to_node": "n2"})
        with self.assertRaises(api.BadRequest):
            api.build_scenario({"kind": "variable_area", "factor": 1.0})
        with self.assertRaises(api.BadRequest):
            api.build_scenario(
                {"kind": "add_load", "node_id": "n3", "down_kips": 0, "east_kips": 0}
            )

    def test_scenario_ids_come_from_the_frozen_set(self):
        built = api.build_scenario({"kind": "scenario", "scenario_id": "s01-thin-m7-demo"})
        self.assertEqual(built.id, "s01-thin-m7-demo")
        with self.assertRaises(api.BadRequest):
            api.build_scenario({"kind": "scenario", "scenario_id": "../../../etc/passwd"})

    def test_the_generated_scenario_id_never_contains_caller_text(self):
        """The id reaches a branch name and a graph id, so it must stay inside
        an alphabet we choose -- not one a request supplies."""
        built = api.build_scenario({"kind": "resize", "member_id": "m7", "area_in2": 6.0})
        self.assertEqual(built.id, "web-resize-m7")
        graph = built.build().after
        self.assertTrue(graph.source.branch_name.startswith("earl-eval-web-"))


class TestRunChange(unittest.TestCase):
    def test_the_demo_change_finds_the_domino(self):
        result = api.run_change({"kind": "resize", "member_id": "m7", "area_in2": 6.0})
        self.assertEqual(result["decision"]["outcome"], Outcome.ESCALATED.value)
        self.assertEqual(result["decision"]["violating_member_ids"], ["m5"])
        # The whole thesis, as a field: the member that failed is not the one
        # the change named.
        self.assertEqual(result["dropped_domino"]["downstream_failures"], ["m5"])
        self.assertEqual(result["change"]["edited_member_ids"], ["m7"])
        self.assertEqual(result["dropped_domino"]["missed"], [])

    def test_every_change_kind_runs_end_to_end(self):
        cases = [
            {"kind": "resize", "member_id": "m3", "area_in2": 17.4},
            {"kind": "remove", "member_id": "m6"},
            {"kind": "add_load", "node_id": "n3", "down_kips": 60, "east_kips": 0},
            {"kind": "move_load", "from_node": "n2", "to_node": "n1"},
            {"kind": "variable_load", "after_kips": 130},
            {"kind": "variable_area", "factor": 0.8},
            {"kind": "scenario", "scenario_id": "s03-thin-m1-severe"},
        ]
        for payload in cases:
            with self.subTest(kind=payload["kind"]):
                result = api.run_change(payload)
                self.assertNotIn("refused", result)
                self.assertIn(result["decision"]["outcome"], {"approved", "escalated"})
                self.assertEqual(len(result["decision"]["members"]), len(MEMBER_IDS) - (
                    1 if payload["kind"] == "remove" else 0))
                self.assertTrue(result["svg"].startswith("<?xml") or "<svg" in result["svg"])
                self.assertIn("ENGINEERING CHANGE NOTICE", result["ecn"]["text"].upper())

    def test_a_safe_change_is_approved(self):
        """A system that escalated everything would be no use. Half the eval
        set is safe for the same reason."""
        result = api.run_change({"kind": "variable_area", "factor": 1.5})
        self.assertEqual(result["decision"]["outcome"], Outcome.APPROVED.value)
        self.assertEqual(result["dropped_domino"]["unsafe_members"], [])

    def test_ground_truth_is_computed_not_taken_from_the_decision(self):
        """`dropped_domino.unsafe_members` comes from `scoreboard.ground_truth`
        over the after-graph, independently of what the pipeline concluded. If
        the two ever disagree the site would show it."""
        result = api.run_change({"kind": "resize", "member_id": "m7", "area_in2": 6.0})
        self.assertEqual(
            sorted(result["dropped_domino"]["unsafe_members"]),
            sorted(result["decision"]["violating_member_ids"]),
        )

    def test_members_are_sorted_worst_first(self):
        rows = api.run_change({"kind": "resize", "member_id": "m7", "area_in2": 6.0})
        factors = [r["safety_factor"] for r in rows["decision"]["members"] if r["safety_factor"]]
        self.assertEqual(factors, sorted(factors))

    def test_raising_the_threshold_is_allowed(self):
        result = api.run_change(
            {"kind": "resize", "member_id": "m7", "area_in2": 6.0, "threshold": 1.5}
        )
        self.assertEqual(result["decision"]["threshold"], 1.5)
        self.assertIn("m7", result["decision"]["violating_member_ids"])

    def test_lowering_the_floor_is_refused_by_the_gate(self):
        """Not by this layer. The message must be the one `resolve_threshold`
        raises, so what a judge sees on the page is the real rule."""
        result = api.run_change(
            {"kind": "resize", "member_id": "m7", "area_in2": 6.0, "threshold": 0.5}
        )
        self.assertTrue(result["refused"])
        self.assertIn("below the floor", result["detail"])
        self.assertIn("can never be auto-approved", result["detail"])

    def test_a_run_writes_nothing(self):
        """`run_pipeline` is called with no report dir and no sender, and
        nothing here calls `PipelineResult.write`. A browser cannot make the
        server touch the disk."""
        before = sorted(p.name for p in (ROOT / "artifacts").glob("*")) if (
            ROOT / "artifacts").exists() else []
        api.run_change({"kind": "resize", "member_id": "m7", "area_in2": 6.0})
        after = sorted(p.name for p in (ROOT / "artifacts").glob("*")) if (
            ROOT / "artifacts").exists() else []
        self.assertEqual(before, after)


class TestTamper(unittest.TestCase):
    """Act 4, as a button."""

    def setUp(self):
        self.run = api.run_change({"kind": "resize", "member_id": "m7", "area_in2": 6.0})

    def test_approving_a_failed_decision_raises(self):
        out = api.tamper({"run_id": self.run["run_id"]})
        self.assertTrue(out["enforced"])
        self.assertEqual(out["exception"], "ValueError")
        self.assertTrue(any("0.73" in r for r in out["reasons"]))
        self.assertTrue(any("can never be auto-approved" in r for r in out["reasons"]))

    def test_it_acts_on_the_decision_the_run_produced(self):
        """The browser sends a run id, never a Decision. Anything else and a
        request could hand us a decision engineered to validate."""
        with self.assertRaises(api.BadRequest):
            api.tamper({"decision": {"outcome": "approved"}})
        with self.assertRaises(api.BadRequest):
            api.tamper({"run_id": "0" * 12})

    def test_an_approvable_decision_is_not_melodramatic(self):
        """A genuinely safe change approving is not a hole in the guarantee,
        and the site must not dress it up as one."""
        safe = api.run_change({"kind": "variable_area", "factor": 1.5})
        out = api.tamper({"run_id": safe["run_id"], "outcome": "approved"})
        self.assertFalse(out["enforced"])
        self.assertTrue(out["legitimate"])

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaises(api.BadRequest):
            api.tamper({"run_id": self.run["run_id"], "outcome": "merged"})

    def test_the_store_forgets_the_oldest_runs(self):
        store = api.RunStore(limit=3)
        ids = [store.put(api.Decision.from_json(
            api.STORE.get(self.run["run_id"]).to_json())) for _ in range(4)]
        with self.assertRaises(api.BadRequest):
            store.get(ids[0])
        self.assertIsNotNone(store.get(ids[-1]))


class TestScoreboard(unittest.TestCase):
    def test_reports_the_tie_as_measured(self):
        board = api.scoreboard()
        if not board["available"]:
            self.skipTest("no recordings installed")
        by_agent = {a["agent"]: a for a in board["agents"]}
        self.assertIn("baseline", by_agent)
        self.assertIn("system", by_agent)
        # The negative result. If someone ever "fixes" this number, the test
        # should be what stops them.
        self.assertEqual(by_agent["baseline"]["dropped_dominoes"], 0)
        self.assertEqual(by_agent["system"]["dropped_dominoes"], 0)


# ==========================================================================
# The HTTP layer
# ==========================================================================

class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_the_limit_then_refuses(self):
        limiter = RateLimiter(limit=3, window=60.0)
        self.assertEqual([limiter.allow("a") for _ in range(4)], [True, True, True, False])

    def test_clients_are_counted_separately(self):
        limiter = RateLimiter(limit=1, window=60.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("a"))

    def test_the_table_does_not_grow_without_bound(self):
        limiter = RateLimiter(limit=1, window=0.0)
        for i in range(2500):
            limiter.allow(f"client-{i}")
        self.assertLess(len(limiter._hits), 2500)


class _ServerCase(unittest.TestCase):
    """One real server on an ephemeral port for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls._logging = web_app.LOG_REQUESTS
        web_app.LOG_REQUESTS = False       # a suite is not a demo console
        cls.server = serve("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web_app.LOG_REQUESTS = cls._logging

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=20) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def post(self, path, body, *, raw=False):
        data = body if raw else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def raw_request(self, line):
        """A request the client library would otherwise normalise for us."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(f"GET {line} HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n".encode())
            return sock.recv(4096).decode("latin-1")
        finally:
            sock.close()


class TestRoutes(_ServerCase):
    def test_the_page_and_its_two_assets_are_served(self):
        for path, needle in [
            ("/", b"<title>EARL"),
            ("/index.html", b"Change bench"),
            ("/app.js", b"function render"),
            ("/style.css", b"--bg:"),
            ("/favicon.svg", b"<svg"),
        ]:
            with self.subTest(path=path):
                status, body, _ = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn(needle, body)

    def test_model_health_and_scoreboard(self):
        for path in ["/api/model", "/api/health", "/api/scoreboard"]:
            status, body, headers = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn("application/json", headers["Content-Type"])
            json.loads(body)

    def test_security_headers_on_every_response(self):
        _, _, headers = self.get("/")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_no_cors_header_is_offered(self):
        """The page and the API are the same origin. Handing out
        Access-Control-Allow-Origin would let any site drive a tunnelled
        server from a visitor's browser."""
        _, _, headers = self.get("/api/model")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_unknown_routes_are_404(self):
        for path in ["/nope", "/api/", "/api/run", "/admin"]:
            self.assertEqual(self.get(path)[0], 404)


class TestStaticFilesAreAFixedList(_ServerCase):
    """There is no path handling to get wrong: a request names a key in
    `_STATIC` or it gets a 404 before the filesystem is touched."""

    def test_dotted_paths_however_spelled(self):
        for line in [
            "/../.env",
            "/../../.env",
            "/..%2f..%2f.env",
            "/%2e%2e%2f.env",
            "/static/..%5c..%5cearl%5cconfig.py",
            "/./../../earl/config.py",
            "//etc/passwd",
            "/app.js%00.txt",
            "/app.js/../../.env",
        ]:
            with self.subTest(line=line):
                head = self.raw_request(line)
                self.assertIn(" 404 ", head.splitlines()[0])
                self.assertNotIn("NGROK", head.upper())
                self.assertNotIn("SKYCIV", head.upper())

    def test_absolute_urls_do_not_reach_a_file(self):
        head = self.raw_request("http://127.0.0.1/style.css")
        self.assertIn("200", head.splitlines()[0])  # the path component is /style.css
        head = self.raw_request("http://evil.test/../../.env")
        self.assertIn(" 404 ", head.splitlines()[0])


class TestBodies(_ServerCase):
    def test_oversized_bodies_are_refused_by_length(self):
        status, payload = self.post("/api/run", b"x" * (MAX_BODY_BYTES + 1), raw=True)
        self.assertEqual(status, 400)
        self.assertIn("under", payload["error"])

    def test_non_json_and_non_object_bodies(self):
        for body in [b"not json", b"[1,2,3]", b'"hello"', b"null"]:
            status, payload = self.post("/api/run", body, raw=True)
            self.assertEqual(status, 400)
            self.assertIn("error", payload)

    def test_a_bad_request_never_leaks_a_traceback(self):
        status, payload = self.post("/api/run", {"kind": "resize", "member_id": "m1"})
        self.assertEqual(status, 400)
        self.assertNotIn("Traceback", json.dumps(payload))
        self.assertNotIn(str(ROOT), json.dumps(payload))


class TestRunOverHttp(_ServerCase):
    def test_the_demo_change_and_then_the_guarantee(self):
        status, result = self.post(
            "/api/run", {"kind": "resize", "member_id": "m7", "area_in2": 6.0}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"]["governing_member_id"], "m5")

        status, tampered = self.post("/api/tamper", {"run_id": result["run_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(tampered["enforced"])

    def test_the_floor_refusal_arrives_as_a_200_with_the_real_message(self):
        """A refusal is not an HTTP error -- it is the answer. The page renders
        it as the guarantee working."""
        status, result = self.post(
            "/api/run",
            {"kind": "resize", "member_id": "m7", "area_in2": 6.0, "threshold": 0.5},
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["refused"])
        self.assertIn("below the floor 1.0", result["detail"])


if __name__ == "__main__":
    unittest.main()
