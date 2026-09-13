"""Orchestrator tests: the LLM proposes, the code disposes. Fully offline.

Every LLM interaction here goes through a fake client or an injected `post`
callable; nothing touches the network and no credential is read.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import build_graph  # noqa: E402

import earl.config  # noqa: E402
from earl.analysis import orchestrator  # noqa: E402
from earl.analysis.orchestrator import (  # noqa: E402
    MAX_LLM_RESPONSE_BYTES,
    AnalysisPlan,
    LLMError,
    LLMPlanner,
    MetaModelClient,
    RuleBasedPlanner,
    build_prompt,
    default_planner,
    parse_plan_json,
)
from earl.contracts import ChangeEvent, ChangeKind, LoadCase, PointLoad  # noqa: E402


def graph_with_moved_load():
    """Two load cases; the change moves load 'p_roof' which lives in the
    SECOND case, so a planner that just takes the first case is wrong."""
    g = build_graph()
    g.load_cases.append(
        LoadCase(
            id="lc_roof",
            name="Roof snow",
            point_loads=[PointLoad("p_roof", "n1", fy=-50_000.0)],
            include_self_weight=True,
        )
    )
    g.change = ChangeEvent(
        id="chg-move",
        kind=ChangeKind.LOAD_MOVED,
        description="Moved roof load from n3 to n1",
        target_id="p_roof",
        value_before="n3",
        value_after="n1",
    )
    g.validate()
    return g


@contextlib.contextmanager
def no_dotenv():
    """Point earl.config at a .env that does not exist.

    `MetaModelClient.from_env()` goes through `earl.config.require`, which
    calls `earl.config.load_env()` directly -- patching the name imported
    into `orchestrator` does not reach it, so without this the tests would
    merge the developer's real `.env` into os.environ and the assertions
    below would depend on whatever LLM_MODEL / LLM_BASE_URL are set there.
    """
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(earl.config, "ENV_PATH", Path(tmp) / "absent.env"):
        yield


class FakeClient:
    def __init__(self, reply=None, error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.reply


class TestAnalysisPlan(unittest.TestCase):
    def test_plan_cannot_carry_a_verdict(self):
        """The LLM-facing object has no field a verdict could travel in."""
        for forbidden in ("threshold", "safety_factor_threshold", "capacity",
                          "status", "outcome", "safety_factor"):
            self.assertFalse(hasattr(AnalysisPlan, forbidden), forbidden)
            self.assertNotIn(forbidden, AnalysisPlan.__dataclass_fields__)
        with self.assertRaises(TypeError):
            AnalysisPlan(load_case_id="lc_1", include_self_weight=False, threshold=0.1)  # type: ignore[call-arg]


class TestRuleBasedPlanner(unittest.TestCase):
    def test_picks_case_containing_the_moved_load(self):
        plan = RuleBasedPlanner().plan(graph_with_moved_load())
        self.assertEqual(plan.load_case_id, "lc_roof")
        self.assertTrue(plan.include_self_weight)   # that case's own flag
        self.assertEqual(plan.source, "rules")
        self.assertIn("p_roof", plan.rationale)

    def test_falls_back_to_first_case_for_a_feature_edit(self):
        g = build_graph()
        plan = RuleBasedPlanner().plan(g)
        self.assertEqual(plan.load_case_id, "lc_1")
        self.assertFalse(plan.include_self_weight)

    def test_focus_is_affected_plus_target_member(self):
        g = build_graph()
        plan = RuleBasedPlanner().plan(g)
        self.assertEqual(plan.focus_member_ids[: len(g.affected_member_ids)],
                         g.affected_member_ids)
        self.assertIn("m5", plan.focus_member_ids)
        self.assertEqual(len(plan.focus_member_ids), len(set(plan.focus_member_ids)))

    def test_non_member_target_is_not_added_to_focus(self):
        plan = RuleBasedPlanner().plan(graph_with_moved_load())
        self.assertNotIn("p_roof", plan.focus_member_ids)

    def test_no_load_cases_raises(self):
        g = build_graph()
        g.load_cases = []
        with self.assertRaises(ValueError):
            RuleBasedPlanner().plan(g)

    def test_deterministic(self):
        a = RuleBasedPlanner().plan(build_graph())
        b = RuleBasedPlanner().plan(build_graph())
        self.assertEqual(a, b)


class TestLLMPlanner(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()

    def test_valid_json_is_accepted_as_llm_plan(self):
        reply = json.dumps({
            "load_case_id": "lc_1",
            "include_self_weight": True,
            "focus_member_ids": ["m5", "m9"],
            "rationale": "m5 was thinned",
        })
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "llm")
        self.assertEqual(plan.load_case_id, "lc_1")
        self.assertTrue(plan.include_self_weight)
        self.assertEqual(plan.focus_member_ids, ["m5", "m9"])
        self.assertNotIn("rejected", plan.rationale)

    def test_code_fences_are_tolerated(self):
        reply = "```json\n" + json.dumps({
            "load_case_id": "lc_1", "include_self_weight": False,
            "focus_member_ids": ["m5"], "rationale": "ok",
        }) + "\n```"
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "llm")

    def test_unknown_focus_ids_are_dropped_not_fatal(self):
        reply = json.dumps({
            "load_case_id": "lc_1", "include_self_weight": False,
            "focus_member_ids": ["m5", "m999", "n1"], "rationale": "ok",
        })
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "llm")
        self.assertEqual(plan.focus_member_ids, ["m5"])
        self.assertIn("m999", plan.rationale)

    def test_junk_falls_back_with_reason(self):
        plan = LLMPlanner(FakeClient("Sure! I think you should check m5.")).plan(self.graph)
        self.assertEqual(plan.source, "rules")
        self.assertTrue(plan.rationale.startswith("LLM plan rejected ("))
        self.assertIn("bad JSON", plan.rationale)
        self.assertEqual(plan.load_case_id, "lc_1")

    def test_unknown_load_case_falls_back_with_reason(self):
        reply = json.dumps({
            "load_case_id": "lc_made_up", "include_self_weight": False,
            "focus_member_ids": [], "rationale": "x",
        })
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "rules")
        self.assertTrue(plan.rationale.startswith("LLM plan rejected ("))
        self.assertIn("lc_made_up", plan.rationale)
        self.assertEqual(plan.load_case_id, "lc_1")

    def test_non_boolean_self_weight_falls_back(self):
        reply = json.dumps({
            "load_case_id": "lc_1", "include_self_weight": "yes",
            "focus_member_ids": [], "rationale": "x",
        })
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "rules")
        self.assertIn("include_self_weight", plan.rationale)

    def test_llm_error_falls_back_with_reason(self):
        plan = LLMPlanner(FakeClient(error=LLMError("HTTP 503"))).plan(self.graph)
        self.assertEqual(plan.source, "rules")
        self.assertTrue(plan.rationale.startswith("LLM plan rejected ("))
        self.assertIn("HTTP 503", plan.rationale)

    def test_unterminated_fence_with_whitespace_does_not_hang(self):
        """Regression for the catastrophic-backtracking fence regex: an
        opening fence followed by a few KB of whitespace must be rejected in
        linear time, not hang the gate."""
        reply = "```json\n" + " " * 20000 + "\n" * 2000
        t0 = time.perf_counter()
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s")
        self.assertEqual(plan.source, "rules")
        self.assertTrue(plan.rationale.startswith("LLM plan rejected ("))
        self.assertEqual(plan.load_case_id, "lc_1")

    def test_oversized_reply_is_rejected_with_reason(self):
        valid = json.dumps({
            "load_case_id": "lc_1", "include_self_weight": False,
            "focus_member_ids": [], "rationale": "x",
        })
        reply = valid + " " * MAX_LLM_RESPONSE_BYTES   # valid JSON, but too big
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "rules")
        self.assertTrue(plan.rationale.startswith("LLM plan rejected ("))
        self.assertIn("too long", plan.rationale)
        self.assertIn(str(MAX_LLM_RESPONSE_BYTES), plan.rationale)

    def test_fallback_equals_rule_plan(self):
        rule = RuleBasedPlanner().plan(self.graph)
        plan = LLMPlanner(FakeClient("{}")).plan(self.graph)
        self.assertEqual(plan.load_case_id, rule.load_case_id)
        self.assertEqual(plan.include_self_weight, rule.include_self_weight)
        self.assertEqual(plan.focus_member_ids, rule.focus_member_ids)

    def test_no_load_cases_is_a_value_error_even_with_llm(self):
        self.graph.load_cases = []
        with self.assertRaises(ValueError):
            LLMPlanner(FakeClient("{}")).plan(self.graph)

    def test_extra_verdict_keys_in_reply_are_ignored(self):
        """A model that tries to smuggle a verdict finds nowhere to put it."""
        reply = json.dumps({
            "load_case_id": "lc_1", "include_self_weight": False,
            "focus_member_ids": [], "rationale": "fine",
            "outcome": "approved", "threshold": 0.1, "status": "pass",
        })
        plan = LLMPlanner(FakeClient(reply)).plan(self.graph)
        self.assertEqual(plan.source, "llm")
        for forbidden in ("outcome", "threshold", "status"):
            self.assertFalse(hasattr(plan, forbidden))

    def test_prompt_shows_only_the_outline(self):
        client = FakeClient("{}")
        LLMPlanner(client).plan(self.graph)
        system, user = client.calls[0]
        for expected in ("feature_edit", "m5", "lc_1", "Benchmark load case",
                         "affected_member_ids", "member_ids"):
            self.assertIn(expected, user)
        # No numbers the model could steer with.
        for hidden in ("yield_strength", "elastic_modulus", "threshold",
                       "capacity", "stress", "444822", "6.895", "\"x\":", "\"fy\":"):
            self.assertNotIn(hidden, user.lower(), hidden)
        self.assertIn("JSON", system)

    def test_build_prompt_is_json_with_listed_fields(self):
        user = build_prompt(self.graph)
        body = user[user.index("{"): user.rindex("}") + 1]
        outline = json.loads(body)
        self.assertEqual(set(outline), {"change", "load_cases", "affected_member_ids", "member_ids"})
        self.assertEqual(set(outline["change"]),
                         {"kind", "description", "target_id", "value_before", "value_after"})
        self.assertEqual(set(outline["load_cases"][0]),
                         {"id", "name", "point_loads", "include_self_weight"})
        self.assertEqual(outline["load_cases"][0]["point_loads"], 2)


class TestParsePlanJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_plan_json('{"a": 1}'), {"a": 1})

    def test_fenced_without_language(self):
        self.assertEqual(parse_plan_json("```\n{\"a\": 1}\n```"), {"a": 1})

    def test_prose_around_object(self):
        self.assertEqual(parse_plan_json('Here: {"a": 1} done'), {"a": 1})

    def test_array_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_plan_json("[1, 2]")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_plan_json("   ")

    def test_fenced_with_surrounding_whitespace(self):
        self.assertEqual(parse_plan_json("  \n```json  \n{\"a\": 1}\n```  \n"), {"a": 1})

    def test_unterminated_fence_is_rejected_quickly(self):
        text = "```json\n" + " " * 20000 + "\n" * 2000
        t0 = time.perf_counter()
        with self.assertRaises(ValueError):
            parse_plan_json(text)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_unterminated_fence_with_object_still_parses(self):
        # Linear stripping: opening fence, no closing fence, JSON inside.
        self.assertEqual(parse_plan_json("```json\n{\"a\": 1}"), {"a": 1})

    def test_length_cap_is_exact(self):
        at_cap = "{" + " " * (MAX_LLM_RESPONSE_BYTES - 2) + "}"
        self.assertEqual(len(at_cap.encode("utf-8")), MAX_LLM_RESPONSE_BYTES)
        self.assertEqual(parse_plan_json(at_cap), {})
        with self.assertRaises(ValueError) as ctx:
            parse_plan_json(at_cap + " ")
        self.assertIn("too long", str(ctx.exception))

    def test_length_cap_counts_bytes_not_characters(self):
        # 3-byte characters: fewer characters than the cap, more bytes.
        text = "{}" + "\u20ac" * (MAX_LLM_RESPONSE_BYTES // 3 + 1)
        self.assertLess(len(text), MAX_LLM_RESPONSE_BYTES)
        with self.assertRaises(ValueError):
            parse_plan_json(text)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class TestMetaModelClient(unittest.TestCase):
    def test_builds_openai_compatible_request(self):
        seen: dict = {}

        def fake_post(url, **kwargs):
            seen["url"] = url
            seen.update(kwargs)
            return FakeResponse(200, {"choices": [{"message": {"content": "{\"ok\": true}"}}]})

        client = MetaModelClient(api_key="test-key", base_url="https://llm.example/v1",
                                 model="muse-test", post=fake_post)
        out = client.complete("SYS", "USER")

        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(seen["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(seen["timeout"], 30)
        body = seen["json"]
        self.assertEqual(body["model"], "muse-test")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "USER"})

    def test_defaults(self):
        client = MetaModelClient(api_key="k")
        self.assertEqual(client.base_url, "https://api.meta.ai/v1")
        self.assertEqual(client.model, "muse-spark-1.1")
        self.assertEqual(client.url, "https://api.meta.ai/v1/chat/completions")
        self.assertEqual(client.timeout, 30)

    def test_http_error_raises_llm_error(self):
        client = MetaModelClient(api_key="k", post=lambda *a, **k: FakeResponse(503, {"error": "down"}))
        with self.assertRaises(LLMError) as ctx:
            client.complete("s", "u")
        self.assertIn("503", str(ctx.exception))

    def test_transport_exception_raises_llm_error(self):
        def boom(*a, **k):
            raise ConnectionError("no route")
        client = MetaModelClient(api_key="k", post=boom)
        with self.assertRaises(LLMError):
            client.complete("s", "u")

    def test_malformed_body_raises_llm_error(self):
        client = MetaModelClient(api_key="k", post=lambda *a, **k: FakeResponse(200, {"nope": []}))
        with self.assertRaises(LLMError):
            client.complete("s", "u")

    def test_repr_does_not_leak_the_api_key(self):
        client = MetaModelClient(api_key="sk-very-secret", model="m")
        for text in (repr(client), str(client)):
            self.assertNotIn("sk-very-secret", text)
            self.assertNotIn("api_key", text)
        self.assertEqual(client.api_key, "sk-very-secret")   # still usable

    def test_from_env_reads_overrides(self):
        env = {"META_MUSE_KEY": "abc", "LLM_BASE_URL": "https://x.example/v2/",
               "LLM_MODEL": "m-9"}
        with no_dotenv(), mock.patch.dict(os.environ, env, clear=False):
            client = MetaModelClient.from_env()
        self.assertEqual(client.api_key, "abc")
        self.assertEqual(client.base_url, "https://x.example/v2")
        self.assertEqual(client.model, "m-9")

    def test_from_env_never_reads_the_real_dotenv(self):
        """With a .env that does not exist and no key in the environment,
        from_env must fail -- proving the test path cannot pick up a
        developer's real credential."""
        with no_dotenv(), mock.patch.dict(os.environ, {"META_MUSE_KEY": ""}, clear=False):
            with self.assertRaises(RuntimeError):
                MetaModelClient.from_env()


class TestDefaultPlanner(unittest.TestCase):
    def test_rules_when_no_key(self):
        with no_dotenv(), mock.patch.dict(os.environ, {"META_MUSE_KEY": ""}, clear=False):
            self.assertIsInstance(default_planner(), RuleBasedPlanner)

    def test_llm_when_key_present(self):
        env = {"META_MUSE_KEY": "k", "LLM_BASE_URL": "https://llm.example/v1",
               "LLM_MODEL": "muse-test"}
        with no_dotenv(), mock.patch.dict(os.environ, env, clear=False):
            planner = default_planner()
        self.assertIsInstance(planner, LLMPlanner)
        self.assertIsInstance(planner.client, MetaModelClient)
        self.assertIsInstance(planner.fallback, RuleBasedPlanner)
        self.assertEqual(planner.client.base_url, "https://llm.example/v1")
        self.assertEqual(planner.client.model, "muse-test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
