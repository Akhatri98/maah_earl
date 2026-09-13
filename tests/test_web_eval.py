"""Public transport limits, shared pipeline, and frozen evaluation evidence."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from earl.contracts import Decision, MemberStatus, Outcome
from earl.eval.baseline import Selection, select_members, verify_selected
from earl.eval.harness import CACHE, cached_results, dropped_dominoes
from earl.ingestion.source import fixture_inputs
from earl.ingestion.truss_map import map_assembly
from earl.pipeline import StageEvent
from earl.scenarios import make_change
from earl.units import IN
from earl import web


class EvalTests(unittest.TestCase):
    def test_frozen_scoreboard_has_twenty_cases_and_reconciles_counts(self):
        result = cached_results()
        self.assertEqual(len(result["scenarios"]), 20)
        self.assertEqual(result["summary"]["earl"]["dropped_dominoes"], 0)
        self.assertEqual(result["summary"]["baseline_label"], "rule-based baseline")
        for system in ("earl", "baseline"):
            count = sum(len(dropped_dominoes(c["ground_truth_failing_ids"], c[system]["reported_member_ids"])) for c in result["scenarios"])
            self.assertEqual(result["summary"][system]["dropped_dominoes"], count)
        for case in result["scenarios"]:
            baseline = json.loads((CACHE / "baseline" / f"{case['id']}.json").read_text())
            truth = json.loads((CACHE / "truth" / f"{case['id']}.json").read_text())
            Decision.from_dict(baseline["decision"]).validate()
            Decision.from_dict(truth["earl_decision"]).validate()

    def test_selection_may_verify_all_and_unselected_numbers_are_hidden(self):
        source = fixture_inputs()
        before = map_assembly(source.assembly, source.variables)
        before.change = make_change(before, "add-load-n2-80")
        after = before.change.apply(before)
        local = select_members(before, after)
        self.assertLess(len(local.member_ids), len(after.members))
        decision = verify_selected(after, local)
        for member in decision.member_results:
            if member.member_id not in local.member_ids:
                self.assertIsNone(member.stress_after)
                self.assertIsNone(member.safety_factor)
                self.assertEqual(member.status, MemberStatus.NOT_EVALUATED)
        all_members = Selection([m.id for m in after.members], "test all-member selection", "No selection cap")
        complete = verify_selected(after, all_members)
        truth = next(c for c in cached_results()["scenarios"] if c["id"] == "add-load-n2-80")
        self.assertEqual(set(complete.violating_member_ids), set(truth["ground_truth_failing_ids"]))


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(web.app)

    def test_catalogue_eval_and_capability_proof_are_offline(self):
        with patch("requests.post") as post, patch("requests.get") as get:
            catalogue = self.client.get("/api/scenarios")
            evaluation = self.client.get("/api/eval")
            proof = self.client.get("/api/source/capabilities")
        post.assert_not_called()
        get.assert_not_called()
        self.assertEqual(catalogue.status_code, 200)
        self.assertEqual(len(catalogue.json()["scenarios"]), 20)
        self.assertTrue(catalogue.json()["demo_mode"])
        self.assertEqual(evaluation.status_code, 200)
        self.assertIn("test_orchestration_has_no_merge_write_or_send_capability", proof.text)

    def test_sse_uses_shared_pipeline_and_serves_saved_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(web, "OUTPUT_ROOT", Path(directory)), patch.dict(os.environ, {"DEMO_MODE": "false"}), patch("requests.post") as post:
            response = self.client.get("/api/stream", params={"scenario": "thin-compression"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
            self.assertEqual([e["stage"] for e in events], list(web.STAGES))
            result = events[-1]["data"]
            self.assertEqual(result["decision"]["outcome"], "escalated")
            self.assertEqual(self.client.get(result["artifacts"]["ecn_url"]).status_code, 200)
            self.assertEqual(self.client.get(result["artifacts"]["email_url"]).status_code, 200)
            self.assertEqual(self.client.get(result["artifacts"]["trace_url"]).status_code, 200)
            self.assertIn("NOT PERFORMED", self.client.get(result["artifacts"]["report_url"]).text)
            post.assert_not_called()

    def test_public_inputs_reject_unknown_nonfinite_and_traversal(self):
        for params in ({"scenario": "unknown"}, {"scenario": "custom-area", "param": '{"area_in2":NaN}'},
                       {"param": '[]'}, {"scenario": "custom-load", "param": '{"node":"n5"}'},
                       {"scenario": "custom-area", "param": '{"area_in2":true}'},
                       {"scenario": "custom-remove", "param": '{"area_in2":8}'},
                       {"scenario": "remove-m1", "param": '{"value":8}'},
                       {"scenario": "custom-area", "param": '{"member":"m11"}'}):
            self.assertEqual(self.client.get("/api/stream", params=params).status_code, 400)
        self.assertEqual(self.client.get("/api/run/not-a-run/trace").status_code, 404)
        self.assertEqual(self.client.get("/api/run/../decision").status_code, 404)
        self.assertEqual(self.client.post("/api/parse", json={"text": "is this safe?"}).status_code, 400)

    def test_parse_returns_typed_edit_before_run(self):
        response = self.client.post("/api/parse", json={"text": "resize m8 to 8 in2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["change"]["field_name"], "area")
        self.assertEqual(response.json()["provenance"], "rule-based parser")

    def test_judge_numeric_values_are_clamped_not_trusted(self):
        graph = web.demo_graph()
        large = make_change(graph, "custom-area", param={"area_in2": 1e100})
        small = make_change(graph, "custom-area", param={"area_in2": -1e100})
        load = make_change(graph, "custom-load", param={"load_kn": 1e100, "node": "n1"})
        self.assertAlmostEqual(large.numeric_after / IN**2, 40)
        self.assertAlmostEqual(small.numeric_after / IN**2, 0.5)
        self.assertEqual(load.numeric_after, -150000)
        large.apply(graph).validate()
        small.apply(graph).validate()
        load.apply(graph).validate()

    def test_stage_event_revalidates_decision_on_produce_and_consume(self):
        truth = json.loads((CACHE / "truth" / "thin-compression.json").read_text())
        forged = truth["earl_decision"]
        forged["outcome"] = "approved"
        payload = {"run_id": "a" * 32, "stage": "gate", "status": "passed",
                   "duration_ms": 1.0, "line": "Untrusted approval", "data": {"decision": forged}}
        with self.assertRaises(ValueError):
            StageEvent(**payload).to_dict()
        with self.assertRaises(ValueError):
            StageEvent.from_dict(payload)

    def test_tampered_trace_is_not_served_as_a_valid_record(self):
        run_id = "b" * 32
        truth = json.loads((CACHE / "truth" / "thin-compression.json").read_text())
        forged = truth["earl_decision"]
        forged["outcome"] = "approved"
        payload = {"run_id": run_id, "stage": "gate", "status": "passed",
                   "duration_ms": 1.0, "line": "Untrusted approval", "data": {"decision": forged}}
        with tempfile.TemporaryDirectory() as directory, patch.object(web, "OUTPUT_ROOT", Path(directory)):
            trace_dir = Path(directory) / "traces"
            trace_dir.mkdir()
            (trace_dir / f"{run_id}.jsonl").write_text(json.dumps(payload) + "\n")
            with self.assertRaises(ValueError):
                self.client.get(f"/api/run/{run_id}/trace")
