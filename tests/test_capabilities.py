"""Architectural proof: the model orchestration surface has no write or merge."""

import ast
import inspect
import unittest

from earl.analysis import llm
from earl.ingestion.onshape_client import OnshapeClient


class OrchestrationCapabilityTests(unittest.TestCase):
    def test_orchestration_has_no_merge_write_or_send_capability(self):
        self.assertEqual(llm.ORCHESTRATION_CAPABILITIES,
                         frozenset({"interpret_change", "select_load_case", "draft_narrative"}))
        self.assertEqual(llm.PROVIDER_TOOLS, ())
        self.assertFalse({"merge", "write", "send", "send_email", "merge_branch"} &
                         llm.ORCHESTRATION_CAPABILITIES)
        source = ast.parse(inspect.getsource(llm))
        for node in ast.walk(source):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith(("earl.delivery", "earl.pipeline")))
            if isinstance(node, ast.Dict):
                keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
                self.assertNotIn("tools", keys)
                self.assertNotIn("functions", keys)
        self.assertFalse(any(name in dir(OnshapeClient) for name in
                             ("merge", "write", "send", "create_branch", "merge_branch")))

    def test_gate_has_no_language_model_or_network_dependency(self):
        from earl.analysis import gate
        source = ast.parse(inspect.getsource(gate))
        for node in ast.walk(source):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("llm", "requests", "earl.analysis.llm"))
