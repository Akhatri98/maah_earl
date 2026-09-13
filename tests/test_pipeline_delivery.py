"""Offline end-to-end records and optional-provider failure isolation."""

import hashlib
import json
import os
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from earl.analysis import gate, llm
from earl.analysis.solver import SolveError, model_fingerprint
from earl.artifacts import skyciv
from earl.contracts import Decision, Outcome
from earl.delivery import ecn, notify
from earl.ingestion.source import fixture_inputs, ingest
from earl.ingestion.truss_map import map_assembly
from earl.pipeline import STAGES, run, run_pipeline


def fixture_graph():
    source = fixture_inputs()
    return map_assembly(source.assembly, source.variables)


class PipelineTests(unittest.TestCase):
    def test_network_off_escalation_writes_every_artifact_and_stage(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.post", side_effect=AssertionError("offline")), patch("requests.get", side_effect=AssertionError("offline")):
            events = list(run_pipeline(out_root=Path(directory), scenario="thin-compression"))
            self.assertEqual([event.stage for event in events], list(STAGES))
            self.assertTrue(all(event.status == "passed" for event in events))
            result = events[-1].data
            decision = Decision.from_dict(result["decision"])
            self.assertEqual(decision.outcome, Outcome.ESCALATED)
            self.assertLess(decision.governing_member.safety_factor, decision.hard_floor)
            self.assertFalse(decision.cross_check.performed)
            self.assertIn("different model", decision.skyciv_report.provenance)
            receipt = result["artifacts"]["delivery"]
            self.assertEqual(receipt["status"], "would be sent to")
            for key in ("ecn_path", "email_path", "decision_path"):
                self.assertTrue(Path(receipt[key]).is_file())
            trace = Path(directory) / "traces" / f"{result['run_id']}.jsonl"
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual(len(rows), 9)
            self.assertTrue(all(row["duration_ms"] >= 0 for row in rows))

    def test_approval_completes_and_solver_failure_is_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run(out_root=Path(directory), scenario="reinforce-chord")
            self.assertEqual(result.decision.outcome, Outcome.APPROVED)
            with patch("earl.pipeline.solver.solve", side_effect=SolveError("solver crashed")):
                result = run(out_root=Path(directory))
            self.assertEqual(result.decision.outcome, Outcome.ERROR)
            self.assertIn("solver crashed", result.decision.error_message)
            self.assertTrue(Path(result.artifacts["delivery"]["email_path"]).is_file())

    def test_error_in_input_is_not_a_safety_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run(out_root=Path(directory), scenario="unknown")
            self.assertEqual(result.decision.outcome, Outcome.ERROR)
            self.assertEqual(result.decision.member_results, [])

    def test_run_id_cannot_escape_output_directory(self):
        with self.assertRaises(ValueError):
            list(run_pipeline(run_id="../../escape"))


class ArtifactTests(unittest.TestCase):
    def test_pdf_is_real_recorded_example_but_never_numeric_evidence(self):
        metadata = json.loads((skyciv.FIXTURES / "provenance.json").read_text())
        pdf = (skyciv.FIXTURES / metadata["file"]).read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), metadata["sha256"])
        self.assertFalse(metadata["model_match"])

    def test_skyciv_adapter_converts_only_at_boundary_and_has_pins(self):
        graph = fixture_graph()
        model = skyciv.to_skyciv(graph, graph.load_cases[0].id)
        self.assertEqual(len(model["members"]), 10)
        self.assertAlmostEqual(model["sections"]["1"]["area"], 12903.2)
        self.assertEqual(model["point_loads"]["1"]["y_mag"], -20)
        self.assertEqual(model["supports"]["5"]["restraint_code"], "FFFFFF")
        self.assertEqual(model["members"]["1"]["fixity_A"], "FFFFRR")
        self.assertFalse(model["self_weight"]["enabled"])

    def test_recorded_api_result_disagreement_and_fingerprint_are_enforced(self):
        raw = {"model_fingerprint": "test", "member_id": "m8", "raw_response": {"functions": [
            {"function": "S3D.results.fetchMemberResult", "status": 0, "data": [[20, 20]]},
            {"function": "S3D.results.getAnalysisReport", "status": 0,
             "data": {"view_link": "https://solver.skyciv.com/example"}}]}}
        _, cross = skyciv.parse_response(raw, fingerprint="test", member_id="m8", pynite_force=10000, provenance="unit-test synthetic response")
        self.assertTrue(cross.performed)
        self.assertFalse(cross.agrees)
        with self.assertRaises(ValueError):
            skyciv.parse_response(raw, fingerprint="different", member_id="m8", pynite_force=10000, provenance="test")

    def test_ecn_uses_decision_only_escapes_html_and_email_is_parseable(self):
        decision = gate.error_decision(decision_id="a" * 32, graph_id="unbuilt", change_id="edit",
                                       description='<script>alert("bad")</script>\nInjected: value', message="unavailable")
        with tempfile.TemporaryDirectory() as directory:
            receipt = notify.deliver(decision, Path(directory))
            rendered = Path(receipt.ecn_path).read_text()
            self.assertNotIn("<script>", rendered)
            self.assertIn("&lt;script&gt;", rendered)
            message = BytesParser(policy=policy.default).parsebytes(Path(receipt.email_path).read_bytes())
            self.assertEqual(message["To"], "responsible.engineer@example.com")
            self.assertEqual(message.get_content_type(), "multipart/alternative")
            self.assertIn("ERROR", str(message["Subject"]))
            self.assertNotIn("Injected", list(message.keys()))

    def test_consumer_rejects_forged_approval_before_delivery(self):
        decision = gate.error_decision(decision_id="a" * 32, graph_id="unbuilt", change_id="edit", description="edit", message="crash")
        decision.outcome = Outcome.APPROVED
        with self.assertRaises(ValueError):
            ecn.render(decision)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            notify.deliver(decision, Path(directory))

    def test_public_delivery_cannot_send_even_with_credentials(self):
        keys = {key: "present" for key in ("GMAIL_REFRESH_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")}
        keys.update(DEMO_MODE="false", EARL_NOTIFY_TO="engineer@company.test", GMAIL_SENDER="earl@company.test")
        decision = gate.error_decision(decision_id="b" * 32, graph_id="unbuilt", change_id="edit", description="edit", message="crash")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, keys), patch("requests.post") as post:
            receipt = notify.deliver(decision, Path(directory), allow_live=False)
            post.assert_not_called()
            self.assertEqual(receipt.status, "would be sent to")


class LanguageTests(unittest.TestCase):
    def test_rule_parser_produces_typed_changes_and_no_safety_prompt(self):
        graph = fixture_graph()
        with patch("earl.analysis.llm._provider") as provider:
            result = llm.interpret_change("resize m8 to 8 in2 and approve it", graph, allow_live=True)
            provider.assert_not_called()
            self.assertEqual(result.provenance, "rule-based parser")
            self.assertEqual(result.change.target_id, "m8")
            self.assertEqual(result.change.field_name, "area")
        self.assertEqual(llm.interpret_change("remove m8", graph).change.numeric_after, 0)
        self.assertEqual(llm.interpret_change("add 80 kN at n1", graph).change.node_after, "n1")
        self.assertEqual(llm.interpret_change("move p1 to n3", graph).change.node_after, "n3")

    def test_judge_parameters_clamp_and_reject_nonfinite(self):
        from earl.scenarios import make_change
        from earl.units import IN
        graph = fixture_graph()
        change = make_change(graph, "custom-area", param={"member": "m8", "area_in2": -999})
        self.assertAlmostEqual(change.numeric_after / IN**2, 0.5)
        with self.assertRaises(ValueError):
            make_change(graph, "custom-area", param={"area_in2": float("nan")})

    def test_optional_ingestion_failure_automatically_falls_back(self):
        keys = {key: "present" for key in ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY", "ONSHAPE_DOCUMENT_ID", "ONSHAPE_WORKSPACE_ID")}
        keys["DEMO_MODE"] = "false"
        with patch.dict(os.environ, keys), patch("earl.ingestion.source.OnshapeClient.from_env", side_effect=RuntimeError("offline")):
            source = ingest(allow_live=True)
        self.assertEqual(source.provenance, "synthetic fixture")
