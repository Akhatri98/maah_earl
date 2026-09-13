"""Escalation tests: SkyCiv artifact attached, verdict never changed. [3B]

Uses the real SkyCivClient with an injected transport and downloader, so the
whole analyze -> report -> cross-check -> validate path runs offline.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import AREA, build_decision, build_graph  # noqa: E402

from earl.artifacts.escalation import escalate  # noqa: E402
from earl.artifacts.skyciv_client import (  # noqa: E402
    DEFAULT_DESIGN_CODE,
    FN_MODEL_SOLVE,
    SkyCivClient,
    SkyCivError,
)
from earl.config import SkyCivConfig  # noqa: E402
from earl.contracts import Decision, MemberStatus, Outcome  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "skyciv" / "synthetic"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_decision() -> Decision:
    """Hand-built ESCALATED decision (m5 governing) with axial forces set to
    the equal-area PyNite forces so the synthetic SkyCiv fixture agrees."""
    forces_lb = {
        "m1": 195364.99, "m2": 40124.63, "m3": -204635.01, "m4": -59875.37,
        "m5": 35489.62, "m6": 40124.63, "m7": 147976.25, "m8": -134866.46,
        "m9": 84676.56, "m10": -56744.80,
    }
    decision = build_decision(build_graph())
    for r in decision.member_results:
        r.axial_force_after = forces_lb[r.member_id] * 4.4482216
        r.stress_after = r.axial_force_after / AREA
    decision.skyciv_report = None       # nothing attached yet
    decision.cross_check.performed = False
    decision.validate()
    return decision


def make_approved() -> Decision:
    decision = make_decision()
    for r in decision.member_results:
        r.safety_factor, r.status, r.utilization = 2.0, MemberStatus.PASS, 0.5
    decision.violating_member_ids = []
    decision.outcome = Outcome.APPROVED
    decision.validate()
    return decision


def make_error() -> Decision:
    decision = make_decision()
    for r in decision.member_results:
        r.safety_factor, r.status = None, MemberStatus.NOT_EVALUATED
    decision.violating_member_ids = []
    decision.outcome = Outcome.ERROR
    decision.error_message = "SolverError: structure is unstable"
    decision.validate()
    return decision


class FakeTransport:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def stub_downloader(url: str, dest: Path) -> Path:
    dest.write_bytes(b"%PDF-1.4 stub for " + url.encode())
    return dest


def make_client(responses, **kwargs) -> tuple[SkyCivClient, FakeTransport]:
    transport = FakeTransport(responses)
    client = SkyCivClient(
        config=SkyCivConfig(username="tester@example.com", key="not-a-real-key"),
        transport=transport,
        downloader=kwargs.pop("downloader", stub_downloader),
        **kwargs,
    )
    return client, transport


def happy_responses() -> list:
    return [load_fixture("solve_response.json"), load_fixture("design_check_response.json")]


class TestEscalateHappyPath(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()
        self.decision = make_decision()

    def test_attaches_report_and_cross_check(self):
        client, transport = make_client(happy_responses())
        out = escalate(self.graph, self.decision, client)

        self.assertIsNot(out, self.decision)
        self.assertIs(out.outcome, Outcome.ESCALATED)
        self.assertEqual(out.violating_member_ids, ["m5"])
        out.validate()

        self.assertIsNotNone(out.skyciv_report)
        self.assertEqual(out.skyciv_report.report_id, "skyciv-dec-001")
        self.assertEqual(out.skyciv_report.url, "https://platform.skyciv.com/reports/synthetic-0001.pdf")
        self.assertEqual(out.skyciv_report.design_code, DEFAULT_DESIGN_CODE)
        self.assertIsNone(out.skyciv_report.local_path)          # no report_dir
        self.assertIsNotNone(out.skyciv_report.generated_at)
        self.assertIn("T", out.skyciv_report.generated_at)

        self.assertTrue(out.cross_check.performed)
        self.assertEqual(out.cross_check.member_id, "m5")
        self.assertTrue(out.cross_check.agrees)
        self.assertLess(out.cross_check.relative_difference, 1e-4)

        # Analysed the decision's load case with the requested design code.
        self.assertEqual(len(transport.payloads), 2)
        s3d = transport.payloads[0]["functions"][1]["arguments"]["s3d_model"]
        self.assertEqual(s3d["load_combinations"]["1"]["name"], "lc_1")

    def test_input_decision_is_not_mutated(self):
        before = self.decision.to_dict()
        client, _ = make_client(happy_responses())
        escalate(self.graph, self.decision, client)
        self.assertEqual(self.decision.to_dict(), before)

    def test_report_dir_downloads_via_injected_downloader(self):
        client, _ = make_client(happy_responses())
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "reports"
            out = escalate(self.graph, self.decision, client, report_dir=report_dir)
            path = out.skyciv_report.local_path
            self.assertIsInstance(path, str)
            self.assertEqual(Path(path), report_dir / "skyciv-dec-001.pdf")
            self.assertTrue(Path(path).exists())
            self.assertTrue(Path(path).read_bytes().startswith(b"%PDF"))
        out.validate()

    def test_download_failure_keeps_url_and_surfaces(self):
        def broken(url: str, dest: Path) -> Path:
            raise OSError("disk full")

        client, _ = make_client(happy_responses(), downloader=broken)
        with tempfile.TemporaryDirectory() as tmp:
            out = escalate(self.graph, self.decision, client, report_dir=Path(tmp))
        self.assertIsNotNone(out.skyciv_report.url)
        self.assertIsNone(out.skyciv_report.local_path)
        self.assertTrue(out.cross_check.performed)
        self.assertIn("download failed", out.result("m5").note)
        self.assertIn("disk full", out.result("m5").note)

    def test_design_code_none_passes_through(self):
        client, transport = make_client([load_fixture("solve_response.json")])
        out = escalate(self.graph, self.decision, client, design_code=None)
        self.assertIsNone(out.skyciv_report.design_code)
        self.assertEqual(len(transport.payloads), 1)

    def test_run_warnings_are_surfaced(self):
        response = load_fixture("solve_response.json")
        del response["last_session_id"]
        client, _ = make_client([response])
        out = escalate(self.graph, self.decision, client)
        self.assertIn("SkyCiv warnings", out.result("m5").note)


class TestEscalateSkipsWhenNotNeeded(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()

    def test_approved_untouched(self):
        decision = make_approved()
        client, transport = make_client(happy_responses())
        out = escalate(self.graph, decision, client)
        self.assertIs(out, decision)
        self.assertIsNone(out.skyciv_report)
        self.assertEqual(transport.payloads, [])

    def test_approved_runs_with_always(self):
        decision = make_approved()
        client, transport = make_client(happy_responses())
        out = escalate(self.graph, decision, client, always=True)
        self.assertIsNot(out, decision)
        self.assertIs(out.outcome, Outcome.APPROVED)
        self.assertIsNotNone(out.skyciv_report)
        self.assertTrue(out.cross_check.performed)
        self.assertEqual(len(transport.payloads), 2)
        out.validate()

    def test_error_untouched_even_with_always(self):
        decision = make_error()
        client, transport = make_client(happy_responses())
        out = escalate(self.graph, decision, client, always=True)
        self.assertIs(out, decision)
        self.assertIs(out.outcome, Outcome.ERROR)
        self.assertEqual(transport.payloads, [])

    def test_invalid_decision_rejected_up_front(self):
        decision = make_decision()
        decision.outcome = Outcome.APPROVED       # below threshold: illegal
        client, _ = make_client(happy_responses())
        with self.assertRaises(ValueError):
            escalate(self.graph, decision, client)


class TestEscalateFailure(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()

    def assert_unavailable(self, out: Decision, original: Decision, fragment: str) -> None:
        self.assertIs(out.outcome, original.outcome)
        self.assertEqual(out.violating_member_ids, original.violating_member_ids)
        self.assertIsNone(out.skyciv_report)
        self.assertFalse(out.cross_check.performed)
        self.assertEqual(out.cross_check.member_id, "m5")
        note = out.result("m5").note
        self.assertIn("SkyCiv unavailable: ", note)
        self.assertIn(fragment, note)
        out.validate()

    def test_function_failure_surfaces_and_keeps_outcome(self):
        decision = make_decision()
        client, _ = make_client([load_fixture("solve_failed_response.json")])
        out = escalate(self.graph, decision, client)
        self.assert_unavailable(out, decision, FN_MODEL_SOLVE)
        self.assertIn("unstable", out.result("m5").note)

    def test_network_failure_surfaces(self):
        decision = make_decision()
        client, _ = make_client([requests.ConnectionError("no route to host")])
        out = escalate(self.graph, decision, client)
        self.assert_unavailable(out, decision, "no route to host")

    def test_existing_note_preserved_and_appended(self):
        decision = make_decision()
        decision.result("m5").note = "unsafe but not in graph.affected_member_ids"
        client, _ = make_client([SkyCivError("HTTP 503: maintenance", status=503)])
        out = escalate(self.graph, decision, client)
        note = out.result("m5").note
        self.assertTrue(note.startswith("unsafe but not in graph.affected_member_ids; SkyCiv unavailable: "))
        self.assertIn("maintenance", note)
        # Other members untouched.
        self.assertIsNone(out.result("m1").note)
        # And the original decision's note is unchanged.
        self.assertEqual(decision.result("m5").note, "unsafe but not in graph.affected_member_ids")

    def test_failure_with_always_on_approved_keeps_approved(self):
        decision = make_approved()
        client, _ = make_client([requests.Timeout("timed out")])
        out = escalate(self.graph, decision, client, always=True)
        self.assertIs(out.outcome, Outcome.APPROVED)
        self.assertFalse(out.cross_check.performed)
        self.assertIn("SkyCiv unavailable: ", out.governing_member.note)
        out.validate()

    def test_missing_load_case_is_unavailable_not_a_crash(self):
        decision = make_decision()
        decision.load_case_id = "lc_missing"
        client, transport = make_client(happy_responses())
        out = escalate(self.graph, decision, client)
        self.assert_unavailable(out, decision, "lc_missing")
        self.assertEqual(transport.payloads, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
