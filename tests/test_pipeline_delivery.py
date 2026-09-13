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
from earl.contracts import Decision, DependencyGraph, Outcome
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

    def test_missing_or_untrusted_pdf_link_cannot_hide_disagreement(self):
        for report_data in ({}, {"view_link": "javascript:alert(1)"}, {"view_link": None}):
            raw = {"model_fingerprint": "test", "member_id": "m8", "raw_response": {"functions": [
                {"function": "S3D.results.fetchMemberResult", "status": 0, "data": [[20, 20]]},
                {"function": "S3D.results.getAnalysisReport", "status": 0, "data": report_data}]}}
            report, cross = skyciv.parse_response(raw, fingerprint="test", member_id="m8",
                                                  pynite_force=10000, provenance="unit-test synthetic response")
            self.assertFalse(cross.agrees)
            self.assertTrue(cross.performed)
            self.assertIsNone(report.url)
            with tempfile.TemporaryDirectory() as directory:
                skyciv._write_reference(Path(directory), report, cross)
                self.assertNotIn("javascript:", (Path(directory) / "report.html").read_text())

    def test_report_function_and_disk_failure_cannot_erase_a_numerical_disagreement(self):
        from earl.eval.harness import CACHE
        truth = json.loads((CACHE / "truth" / "reinforce-chord.json").read_text())
        graph = DependencyGraph.from_dict(truth["after_graph"])
        decision = Decision.from_dict(truth["earl_decision"])
        self.assertIs(decision.outcome, Outcome.APPROVED)
        keys = {"DEMO_MODE": "false", "SKYCIV_API_USERNAME": "unit-test-only", "SKYCIV_API_KEY": "unit-test-only"}
        response = {"functions": [
            {"function": "S3D.model.solve", "status": 0, "data": None},
            {"function": "S3D.results.fetchMemberResult", "status": 0, "data": [[0, 0]]},
            {"function": "S3D.results.getAnalysisReport", "status": 1, "data": None}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(skyciv, "PROJECT_ROOT", Path(directory)), patch.dict(os.environ, keys), patch("requests.post") as post, patch.object(skyciv, "_write_reference", side_effect=OSError("disk unavailable")):
            post.return_value.json.return_value = response
            report, cross = skyciv.create_report(graph, decision, Path(directory) / "run", allow_live=True)
            final = gate.attach_cross_check(decision, report, cross)
        self.assertIs(final.outcome, Outcome.ESCALATED)
        self.assertFalse(final.cross_check.agrees)
        self.assertIn("write failed", final.skyciv_report.note)

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
            self.assertIsNotNone(message["Date"])
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

    def test_gmail_failure_keeps_the_real_local_email(self):
        keys = {key: "unit-test-only" for key in ("GMAIL_REFRESH_TOKEN", "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET")}
        keys.update(DEMO_MODE="false", GMAIL_NOTIFY_RECIPIENT="engineer@company.test", GMAIL_SENDER_ADDRESS="earl@company.test")
        decision = gate.error_decision(decision_id="d" * 32, graph_id="g", change_id="c", description="edit", message="test error")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, keys), patch("requests.post", side_effect=ConnectionError("offline")) as post:
            receipt = notify.deliver(decision, Path(directory), allow_live=True)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(receipt.status, "would be sent to")
            self.assertIn("could not be confirmed", receipt.note)
            self.assertTrue(Path(receipt.email_path).is_file())

    def test_skyciv_network_failure_uses_explicit_different_model_fixture(self):
        from earl.eval.harness import CACHE
        truth = json.loads((CACHE / "truth" / "thin-compression.json").read_text())
        graph = DependencyGraph.from_dict(truth["after_graph"])
        decision = Decision.from_dict(truth["earl_decision"])
        keys = {"DEMO_MODE": "false", "SKYCIV_API_USERNAME": "unit-test-only", "SKYCIV_API_KEY": "unit-test-only"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, keys), patch("requests.post", side_effect=ConnectionError("offline")) as post:
            report, cross = skyciv.create_report(graph, decision, Path(directory), allow_live=True)
            self.assertEqual(post.call_count, 1)
            self.assertFalse(cross.performed)
            self.assertIsNone(cross.agrees)
            self.assertIn("different model", report.provenance)
            self.assertTrue((Path(directory) / "report.html").is_file())


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

    def test_safety_questions_never_reach_the_optional_provider(self):
        graph = fixture_graph()
        with patch("earl.analysis.llm._provider") as provider:
            for question in ("is this safe?", "is m8 unsafe?", "will m8 fail?", "is this compliant?"):
                with self.assertRaises(ValueError):
                    llm.interpret_change(question, graph, allow_live=True)
            provider.assert_not_called()

    def test_narrative_job_does_not_forward_a_safety_question_either(self):
        decision = gate.error_decision(decision_id="f" * 32, graph_id="g", change_id="c",
                                       description="Is this safe?", message="test error")
        with patch("earl.analysis.llm._provider") as provider:
            _, provenance = llm.draft_narrative(decision, allow_live=True)
        provider.assert_not_called()
        self.assertEqual(provenance, "recorded deterministic template")

    def test_malformed_load_case_selection_falls_back(self):
        graph = fixture_graph()
        for invalid in ([], {}, 1, None, "unknown"):
            with patch("earl.analysis.llm._provider", return_value={"load_case_id": invalid}):
                case, provenance = llm.select_load_case(graph, "resize a member", allow_live=True)
            self.assertEqual(case, "lc_1")
            self.assertEqual(provenance, "deterministic case selection")

    def test_provider_prose_cannot_introduce_a_safety_claim_or_replace_the_edit(self):
        decision = gate.error_decision(decision_id="c" * 32, graph_id="g", change_id="c",
                                       description="Resize m8 from 20 to 8 in^2.", message="test error")
        for claim in ("The structure is sound.", "This change passes the requirements.", "It is acceptable.", ""):
            with patch("earl.analysis.llm._provider", return_value={"narrative": claim}):
                narrative, provenance = llm.draft_narrative(decision, allow_live=True)
            self.assertEqual(provenance, "recorded deterministic template")
            self.assertIn(decision.change_description, narrative)
        with patch("earl.analysis.llm._provider", return_value={"narrative": "The requested edit resizes member m8 from 20 to 8 in^2."}):
            decision.narrative, decision.narrative_provenance = llm.draft_narrative(decision, allow_live=True)
        self.assertTrue(decision.narrative_provenance.startswith("provider"))
        self.assertIn(decision.change_description, ecn.text(decision))
        self.assertIn("Non-authoritative change summary", ecn.text(decision))
        self.assertIs(decision.outcome, Outcome.ERROR)

    def test_live_provider_network_error_falls_back_for_every_job(self):
        graph = fixture_graph()
        keys = {"DEMO_MODE": "false", "META_MUSE_KEY": "unit-test-key",
                "META_MUSE_URL": "https://provider.invalid/chat", "META_MUSE_MODEL": "test-model"}
        decision = gate.error_decision(decision_id="c" * 32, graph_id="g", change_id="c",
                                       description="Resize m8 from 20 to 8 in^2.", message="test error")
        with tempfile.TemporaryDirectory() as directory, patch.object(llm, "PROJECT_ROOT", Path(directory)), patch.dict(os.environ, keys), patch("requests.post", side_effect=ConnectionError("offline")) as post:
            parsed = llm.interpret_change("resize m8 to 8 in2", graph, allow_live=True)
            case, provenance = llm.select_load_case(graph, "resize a member", allow_live=True)
            narrative, source = llm.draft_narrative(decision, allow_live=True)
        self.assertEqual(post.call_count, 3)
        self.assertEqual(parsed.provenance, "rule-based parser")
        self.assertEqual(case, "lc_1")
        self.assertEqual(provenance, "deterministic case selection")
        self.assertEqual(source, "recorded deterministic template")

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
