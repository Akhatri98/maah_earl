"""A documentation-shaped SkyCiv mock must not be mislabeled live evidence."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from earl.artifacts import skyciv
from earl.contracts import CrossCheck, Decision, SkyCivReport
from earl.eval.harness import CACHE
from earl.pipeline import run
from earl.watch.crosscheck import finalize
from earl.watch.runner import ChangeSource, Runner
from earl.watch.state import Ledger


class AgentCrossCheckTests(unittest.TestCase):
    def test_disagreement_escalates_before_autonomous_notice_and_retains_trace(self):
        cross = CrossCheck(member_id="m8", pynite_value=100, skyciv_value=150,
                           provenance="synthetic unit-test comparison")
        cross.evaluate()
        report = SkyCivReport(report_id="unit-test-not-live", provenance="synthetic unit-test comparison")
        with tempfile.TemporaryDirectory() as directory, patch("earl.watch.crosscheck.create_report", return_value=(report, cross)), patch("requests.post") as post:
            root = Path(directory)
            runner = Runner(Ledger(root / "agent" / "state.json"), out_root=root, allow_live=True)
            record = runner.handle_change(ChangeSource(mode="webhook", scenario="reinforce-chord",
                                                      provenance="live Onshape mocked test source"))
            self.assertEqual(record.outcome, "escalated")
            self.assertEqual(record.notice, "ESCALATION")
            supplement = json.loads((root / "runs" / record.run_id / "agent-cross-check.json").read_text())
            self.assertEqual(supplement["pipeline_outcome"], "approved")
            self.assertEqual(supplement["agent_outcome"], "escalated")
            self.assertEqual(supplement["cross_check"]["tolerance"], .05)
            Decision.from_dict(supplement["decision"]).validate()
            trace = (root / "traces" / f"{record.run_id}.jsonl").read_text().splitlines()
            self.assertEqual(len(trace), 9)
            post.assert_not_called()

    def test_absent_credentials_remain_explicit_different_model_fixture(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SKYCIV_API_KEY": "", "SKYCIV_API_USERNAME": ""}), patch("requests.post") as post:
            result = run(scenario="reinforce-chord", out_root=Path(directory))
            result = finalize(result, Path(directory), allow_live=True)
            self.assertFalse(result.decision.cross_check.performed)
            self.assertIn("different model", result.decision.skyciv_report.provenance)
            post.assert_not_called()

    def test_unexpected_actual_response_shape_is_saved_but_not_accepted(self):
        from earl.contracts import DependencyGraph
        truth = json.loads((CACHE / "truth" / "reinforce-chord.json").read_text())
        graph, decision = DependencyGraph.from_dict(truth["after_graph"]), Decision.from_dict(truth["earl_decision"])
        keys = {"DEMO_MODE": "false", "SKYCIV_API_KEY": "unit-test-only", "SKYCIV_API_USERNAME": "unit-test-only"}
        raw = {"unexpected": "synthetic test response, NOT from SkyCiv"}
        with tempfile.TemporaryDirectory() as directory, patch.object(skyciv, "PROJECT_ROOT", Path(directory)), patch.dict(os.environ, keys), patch("requests.post") as post:
            post.return_value.json.return_value = raw
            _, cross = skyciv.create_report(graph, decision, Path(directory) / "run", allow_live=True)
            self.assertFalse(cross.performed)
            records = list((Path(directory) / "out" / "recordings" / "skyciv").glob("*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text())["raw_response"], raw)
