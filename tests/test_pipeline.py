"""Sprint 4 tests: the two tracks, wired together.

Everything here crosses the Track A <-> Track B boundary on purpose. The
first class is the "fit" check the integration sprint exists for: the two
graph builders, the Sprint 0 self-check and the Sprint 3A mock must describe
the same material, or every safety factor depends on who built the graph.
The rest run the whole pipeline offline -- real walker, real PyNite, real
Biject, real ECN, a fake SkyCiv transport and a fake mail sender.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import build_graph as selfcheck_graph  # noqa: E402

from earl.analysis.benchmark import (  # noqa: E402
    DEMO_ONE_HOP_MEMBER_IDS,
    DEMO_TWO_HOP_MEMBER_IDS,
    build_ten_bar_graph,
    demo_before_graph,
    demo_change_graph,
)
from earl.analysis.gate import run_fast_gate  # noqa: E402
from earl.artifacts.skyciv_client import SkyCivClient, SkyCivError  # noqa: E402
from earl.config import SkyCivConfig  # noqa: E402
from earl.contracts import (  # noqa: E402
    CONTRACT_VERSION,
    ChangeEvent,
    ChangeKind,
    Decision,
    EdgeKind,
    MemberStatus,
    OnshapeRef,
    Outcome,
)
from earl.delivery import mock_decisions as mk  # noqa: E402
from earl.delivery.ecn import ECNStatus, build_ecn  # noqa: E402
from earl.delivery.gmail import DeliveryReceipt, OutboxSender  # noqa: E402
from earl.ingestion.benchmark import IN, benchmark_spec  # noqa: E402
from earl.ingestion.changes import change_id, describe_variable_change  # noqa: E402
from earl.ingestion.graph_builder import build_graph  # noqa: E402
from earl.ingestion.walker import walk  # noqa: E402
from earl.pipeline import PipelineResult, enrich_decision, run_pipeline  # noqa: E402

RECORDED = ROOT / "tests" / "fixtures" / "onshape" / "assemblies_d_w_e.json"
SKYCIV_FIXTURES = ROOT / "tests" / "fixtures" / "skyciv" / "synthetic"


def rel_close(a: float, b: float, tol: float = 1e-3) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b))


class FakeSender:
    """Records what the pipeline hands it; never touches the network."""

    channel = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.messages = []
        self.fail = fail

    def send(self, msg) -> DeliveryReceipt:
        self.messages.append(msg)
        if self.fail:
            raise RuntimeError("smtp is on fire")
        return DeliveryReceipt(
            delivered=True, channel="fake", to=str(msg["To"]), subject=str(msg["Subject"]),
            attachments=[p.get_filename() for p in msg.iter_attachments()], message_id="fake-1",
        )


def skyciv_client(responses: list) -> SkyCivClient:
    """A SkyCiv client whose transport replays canned responses (or raises)."""
    answers = list(responses)

    def transport(payload):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return SkyCivClient(SkyCivConfig(username="u", key="k"), transport=transport)


def synthetic(name: str) -> dict:
    return json.loads((SKYCIV_FIXTURES / name).read_text(encoding="utf-8"))


def skyciv_ok() -> SkyCivClient:
    """Solve + design check answered from the synthetic fixtures (two calls)."""
    return skyciv_client([synthetic("solve_response.json"), synthetic("design_check_response.json")])


# --------------------------------------------------------------------------
# 1. The fit: one material, whoever built the graph
# --------------------------------------------------------------------------

class TestBuildersAgree(unittest.TestCase):
    """The Sprint 3A handover's blocker: Track A put the 25 ksi allowable in
    `yield_strength`, Track B put 50 ksi yield there, so capacity -- and every
    safety factor -- differed by exactly 2x depending on the graph's origin.
    Contract 0.2.0 carries both numbers; these tests pin that every producer
    now says the same thing."""

    def materials(self):
        return {
            "track_a": benchmark_spec().material,
            "track_b": build_ten_bar_graph().materials[0],
            "mock_3a": mk.mock_graph().materials[0],
            "selfcheck_0": selfcheck_graph().materials[0],
        }

    def test_contract_version_is_0_2(self):
        self.assertEqual(CONTRACT_VERSION, "0.2.0")

    def test_every_producer_declares_the_same_material(self):
        mats = self.materials()
        ref = mats["track_b"]
        for name, mat in mats.items():
            with self.subTest(producer=name):
                self.assertEqual(mat.id, ref.id)
                self.assertTrue(rel_close(mat.elastic_modulus, ref.elastic_modulus))
                self.assertTrue(rel_close(mat.yield_strength, ref.yield_strength))
                self.assertIsNotNone(mat.allowable_stress)
                self.assertTrue(rel_close(mat.allowable_stress, ref.allowable_stress))
                self.assertTrue(rel_close(mat.density, ref.density))

    def test_capacity_comes_from_the_allowable_not_the_yield(self):
        mat = self.materials()["track_b"]
        self.assertAlmostEqual(mat.yield_strength / 6894.757e3, 50.0, places=2)
        self.assertAlmostEqual(mat.allowable_stress / 6894.757e3, 25.0, places=2)
        self.assertEqual(mat.design_stress, mat.allowable_stress)
        decision = run_fast_gate(demo_change_graph(), threshold=1.0)
        for r in decision.member_results:
            self.assertAlmostEqual(r.capacity, mat.allowable_stress)

    def test_same_change_same_verdict_from_either_builder(self):
        """A Track A graph and a Track B graph of the same structure and the
        same change must produce the same safety factors."""
        areas = dict(mk.OPTIMUM_AREAS)
        areas["m7"] = 6.0
        track_a = mk.mock_graph(change=mk.escalated_change(), areas=areas)
        track_b = build_ten_bar_graph(
            areas_in2=[areas[m] for m in ("m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10")],
            graph_id="graph-b-same-change",
        )
        a = run_fast_gate(track_a, threshold=1.0)
        b = run_fast_gate(track_b, threshold=1.0)
        self.assertEqual(a.outcome, b.outcome)
        self.assertEqual(a.violating_member_ids, b.violating_member_ids)
        for ra in a.member_results:
            rb = b.result(ra.member_id)
            self.assertTrue(rel_close(ra.safety_factor, rb.safety_factor, 1e-6), ra.member_id)

    def test_track_a_mock_numbers_are_what_track_b_computes(self):
        """The Sprint 3A mock froze a direct-stiffness solve of m7 7.46 -> 6.00
        at 25 ksi capacity. Under the agreed convention Track B's gate must
        reproduce those literals, which is what lets the mock retire."""
        areas = dict(mk.OPTIMUM_AREAS)
        areas["m7"] = 6.0
        graph = mk.mock_graph(change=mk.escalated_change(), areas=areas)
        real = run_fast_gate(graph, threshold=1.0)
        frozen = mk.escalated()
        self.assertIs(real.outcome, Outcome.ESCALATED)
        self.assertEqual(real.violating_member_ids, frozen.violating_member_ids)
        for r in real.member_results:
            f = frozen.result(r.member_id)
            with self.subTest(member=r.member_id):
                self.assertTrue(rel_close(r.safety_factor, f.safety_factor, 5e-3))
                self.assertTrue(rel_close(r.stress_after, f.stress_after, 5e-3))
                self.assertIs(r.status, f.status)


# --------------------------------------------------------------------------
# 2. The hand-off: what the Decision carries across the boundary
# --------------------------------------------------------------------------

class TestEnrichDecision(unittest.TestCase):
    def test_copies_source_and_walk_provenance(self):
        graph = demo_change_graph()
        result = walk(graph)
        decision = enrich_decision(run_fast_gate(graph, threshold=1.0), graph, result)
        self.assertEqual(decision.source, graph.source)
        m7 = decision.result("m7")
        self.assertEqual(m7.hops_from_change, 0)
        self.assertIsNone(m7.reached_via)
        for mid in DEMO_ONE_HOP_MEMBER_IDS:
            self.assertEqual(decision.result(mid).hops_from_change, 1, mid)
            if mid != "m5":
                self.assertIs(decision.result(mid).reached_via, EdgeKind.TOPOLOGY, mid)
        for mid in DEMO_TWO_HOP_MEMBER_IDS:
            self.assertEqual(decision.result(mid).hops_from_change, 2, mid)
        self.assertEqual(decision.result("m2").hops_from_change, 1)
        self.assertIs(decision.result("m2").reached_via, EdgeKind.LOAD_PATH)
        # m5 has both a TOPOLOGY and a LOAD_PATH edge from m7; the walker's
        # deterministic order (kind value, "load_path" < "topology") decides
        # which label it carries, but 1 hop it is.
        self.assertEqual(decision.result("m5").hops_from_change, 1)
        self.assertIs(decision.result("m5").reached_via, EdgeKind.LOAD_PATH)
        self.assertTrue(all(r.is_affected for r in decision.member_results))

    def test_survives_the_json_round_trip(self):
        graph = demo_change_graph()
        decision = enrich_decision(run_fast_gate(graph, threshold=1.0), graph, walk(graph))
        again = Decision.from_json(decision.to_json())
        self.assertEqual(again.source.element_id, graph.source.element_id)
        self.assertIs(again.result("m1").reached_via, EdgeKind.TOPOLOGY)
        self.assertEqual(again.result("m1").hops_from_change, 1)

    def test_refuses_a_decision_for_another_graph(self):
        graph = demo_change_graph()
        other = demo_before_graph()
        with self.assertRaises(ValueError):
            enrich_decision(run_fast_gate(other, threshold=1.0), graph, None)

    def test_ecn_from_the_enriched_decision_alone_has_source_and_provenance(self):
        """Contract rule 2: a Decision must be sufficient to write the ECN."""
        graph = demo_change_graph()
        decision = enrich_decision(run_fast_gate(graph, threshold=1.0), graph, walk(graph))
        ecn = build_ecn(decision)                      # no graph, no walk
        self.assertIsNotNone(ecn.source)
        self.assertEqual(ecn.source.element_id, graph.source.element_id)
        m1 = next(l for l in ecn.lines if l.member_id == "m1")
        self.assertEqual(m1.provenance_short, "1 hop via topology")
        m7 = next(l for l in ecn.lines if l.member_id == "m7")
        self.assertEqual(m7.provenance_short, "change target")
        self.assertFalse(any("no dependency graph was supplied with this decision, so" in n.lower()
                             for n in ecn.notes))


# --------------------------------------------------------------------------
# 3. End to end, offline
# --------------------------------------------------------------------------

class TestDemoPipeline(unittest.TestCase):
    """The R1 demo through every stage: Track B graph -> walker -> gate ->
    (SkyCiv down) -> ECN -> mail."""

    @classmethod
    def setUpClass(cls):
        cls.sender = FakeSender()
        cls.result = run_pipeline(
            demo_change_graph(),
            before=demo_before_graph(),
            threshold=1.0,
            skyciv=skyciv_client([SkyCivError("connection refused")]),
            sender=cls.sender,
            sender_address="earl@example.com",
            recipient="engineer@example.com",
        )

    def test_verdict(self):
        r = self.result
        self.assertIs(r.outcome, Outcome.ESCALATED)
        self.assertIs(r.status, ECNStatus.HELD)
        self.assertEqual(r.decision.violating_member_ids, ["m5"])
        self.assertIs(r.decision.result("m7").status, MemberStatus.PASS)
        self.assertAlmostEqual(r.decision.result("m5").safety_factor, 0.735, delta=0.735 * 1e-2)
        self.assertIsNotNone(r.decision.result("m5").stress_before)

    def test_walk_populated_the_graph_and_the_decision(self):
        r = self.result
        self.assertEqual(len(r.walk.member_ids), 10)
        self.assertEqual(r.graph.affected_member_ids, r.walk.member_ids)
        self.assertEqual(r.decision.source, r.graph.source)
        self.assertEqual(r.decision.result("m6").hops_from_change, 2)

    def test_skyciv_failure_is_surfaced_not_swallowed(self):
        r = self.result
        self.assertTrue(r.skyciv_attempted)
        self.assertIsNone(r.decision.skyciv_report)
        self.assertFalse(r.decision.cross_check.performed)
        self.assertIn("SkyCiv unavailable", r.decision.result("m5").note)
        self.assertIn("NOT PERFORMED", r.ecn.cross_check_note)
        self.assertTrue(any(n.startswith("skyciv:") for n in r.notes))

    def test_ecn_carries_source_and_provenance(self):
        ecn = self.result.ecn
        self.assertEqual(ecn.source.element_id, self.result.graph.source.element_id)
        m5 = next(l for l in ecn.lines if l.member_id == "m5")
        self.assertTrue(m5.is_governing)
        self.assertIn("1 hop via", m5.provenance_short)
        self.assertIn("m7 -> m5", m5.provenance)
        self.assertIn("m5", self.result.ecn_text)
        self.assertIn("HELD", self.result.ecn_text)

    def test_delivered_with_the_evidence_pack(self):
        d = self.result.delivery
        self.assertIsNotNone(d)
        self.assertTrue(d.delivered)
        self.assertEqual(d.to, "engineer@example.com")
        self.assertIn("HELD", d.subject)
        names = set(d.attachments)
        self.assertIn(f"{self.result.ecn.id}.json", names)
        self.assertIn(f"{self.result.decision.id}.json", names)
        self.assertIn(f"{self.result.ecn.id}.svg", names)
        msg = self.sender.messages[0]
        body = msg.get_body(preferencelist=("plain",)).get_content()
        self.assertEqual(body.strip(), self.result.ecn_text.strip())

    def test_svg_rendered(self):
        self.assertTrue(self.result.svg.lstrip().startswith("<svg"))
        self.assertIn("m5", self.result.svg)

    def test_write_persists_the_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = self.result.write(tmp)
            for kind in ("graph", "decision", "ecn_json", "ecn_text", "ecn_markdown", "svg", "delivery"):
                self.assertTrue(written[kind].is_file(), kind)
            again = Decision.from_json(written["decision"].read_text(encoding="utf-8"))
            again.validate()
            self.assertEqual(again.violating_member_ids, ["m5"])


class TestSkyCivAttached(unittest.TestCase):
    def test_escalation_attaches_report_and_cross_check(self):
        client = skyciv_ok()
        with tempfile.TemporaryDirectory() as tmp:
            result = run_pipeline(
                demo_change_graph(), threshold=1.0, skyciv=client, report_dir=tmp,
            )
        d = result.decision
        self.assertIs(d.outcome, Outcome.ESCALATED)
        self.assertIsNotNone(d.skyciv_report)
        self.assertEqual(d.skyciv_report.report_id, f"skyciv-{d.id}")
        self.assertEqual(d.cross_check.member_id, "m5")
        # The synthetic fixture carries equal-area forces, not the demo's, so
        # the cross-check DISAGREES -- and the ECN must say so, not hide it.
        self.assertTrue(d.cross_check.performed)
        self.assertIn("SkyCiv analysis report", " ".join(result.ecn.evidence))
        if not d.cross_check.agrees:
            self.assertIn("DISAGREE", result.ecn.cross_check_note)
        # Source and provenance survived escalation's JSON copy.
        self.assertEqual(d.source, result.graph.source)
        self.assertEqual(d.result("m1").hops_from_change, 1)

    def test_approved_change_skips_skyciv_unless_asked(self):
        client = skyciv_ok()
        result = run_pipeline(demo_before_graph(), threshold=1.0, skyciv=client)
        self.assertIs(result.outcome, Outcome.APPROVED)
        self.assertFalse(result.skyciv_attempted)
        self.assertEqual(client.request_count, 0)

        client = skyciv_ok()
        result = run_pipeline(demo_before_graph(), threshold=1.0, skyciv=client, skyciv_always=True)
        self.assertTrue(result.skyciv_attempted)
        self.assertIsNotNone(result.decision.skyciv_report)


class TestApprovedPath(unittest.TestCase):
    def test_approved_is_logged_not_mailed_and_merge_hook_runs(self):
        sender = FakeSender()
        merged: list[str] = []

        def merge(decision: Decision) -> str:
            decision.validate()
            merged.append(decision.id)
            return "merged-into-main"

        result = run_pipeline(
            demo_before_graph(), threshold=1.0,
            sender=sender, sender_address="a@x", recipient="b@x", merge_hook=merge,
        )
        self.assertIs(result.outcome, Outcome.APPROVED)
        self.assertIs(result.status, ECNStatus.APPROVED)
        self.assertIsNone(result.delivery)
        self.assertEqual(sender.messages, [])
        self.assertEqual(merged, [result.decision.id])
        self.assertEqual(result.merged, "merged-into-main")

    def test_approved_can_be_mailed_when_asked(self):
        sender = FakeSender()
        result = run_pipeline(
            demo_before_graph(), threshold=1.0,
            sender=sender, sender_address="a@x", recipient="b@x",
            notify_on={Outcome.APPROVED, Outcome.ESCALATED, Outcome.ERROR},
        )
        self.assertIsNotNone(result.delivery)
        self.assertIn("APPROVED", result.delivery.subject)

    def test_merge_hook_never_runs_for_escalated_or_error(self):
        calls: list[str] = []
        result = run_pipeline(demo_change_graph(), threshold=1.0, merge_hook=lambda d: calls.append(d.id))
        self.assertIs(result.outcome, Outcome.ESCALATED)
        self.assertEqual(calls, [])
        self.assertIsNone(result.merged)

        broken = demo_change_graph()
        broken.load_cases = []
        result = run_pipeline(broken, threshold=1.0, merge_hook=lambda d: calls.append(d.id))
        self.assertIs(result.outcome, Outcome.ERROR)
        self.assertEqual(calls, [])


class TestErrorAndFailurePaths(unittest.TestCase):
    def test_solver_error_is_a_blocked_ecn_that_still_gets_delivered(self):
        graph = demo_change_graph()
        graph.load_cases = []
        sender = FakeSender()
        result = run_pipeline(graph, threshold=1.0, sender=sender, sender_address="a@x", recipient="b@x",
                              skyciv=skyciv_client([]))
        self.assertIs(result.outcome, Outcome.ERROR)
        self.assertIs(result.status, ECNStatus.BLOCKED)
        self.assertFalse(result.skyciv_attempted)          # nothing to send to SkyCiv
        self.assertIsNotNone(result.delivery)              # but the engineer hears about it
        self.assertIn("BLOCKED", result.delivery.subject)
        self.assertTrue(all(r.status is MemberStatus.NOT_EVALUATED for r in result.decision.member_results))

    def test_delivery_failure_is_a_note_not_a_crash(self):
        result = run_pipeline(demo_change_graph(), threshold=1.0, sender=FakeSender(fail=True),
                              sender_address="a@x", recipient="b@x")
        self.assertIs(result.outcome, Outcome.ESCALATED)   # the verdict is untouched
        self.assertFalse(result.delivery.delivered)
        self.assertIn("smtp is on fire", result.delivery.error)
        self.assertTrue(any(n.startswith("delivery: FAILED") for n in result.notes))

    def test_outbox_sender_writes_an_eml(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_pipeline(demo_change_graph(), threshold=1.0, sender=OutboxSender(Path(tmp)),
                                  sender_address="a@x", recipient="b@x")
            self.assertEqual(result.delivery.channel, "outbox")
            self.assertTrue(Path(result.delivery.message_id).is_file())

    def test_threshold_misconfiguration_raises_before_anything_runs(self):
        with self.assertRaises(ValueError):
            run_pipeline(demo_change_graph(), threshold=0.5)


# --------------------------------------------------------------------------
# 4. Track A's real graph through Track B's real gate
# --------------------------------------------------------------------------

class TestOnshapeGraphThroughPipeline(unittest.TestCase):
    """The recorded Onshape assembly, assembled by `graph_builder.build_graph`
    (Track A's production path), for a `barArea` variable edit -- the
    pipeline's primary change signal -- through the whole pipeline."""

    @classmethod
    def setUpClass(cls):
        assembly = json.loads(RECORDED.read_text(encoding="utf-8"))
        load = 10.0 * 4448.222                     # a 1 in^2 truss is safe at 10 kips, not 100
        before_expr, after_expr = "1 in^2", "0.75 in^2"
        kind, description = describe_variable_change("barArea", before_expr, after_expr)
        change = ChangeEvent(
            id=change_id("variable", "barArea", before_expr, after_expr),
            kind=kind, description=description, target_id="barArea",
            value_before=before_expr, value_after=after_expr,
        )
        source = OnshapeRef(document_id="d", workspace_id="w", element_id="e", branch_name="earl-eval-test")
        cls.after = build_graph(
            assembly, graph_id="graph-onshape-after", change=change, source=source,
            spec=benchmark_spec(area=0.75 * IN ** 2, load=load),
        ).graph
        cls.before = build_graph(
            assembly, graph_id="graph-onshape-before", change=change, source=source,
            spec=benchmark_spec(area=1.0 * IN ** 2, load=load),
        ).graph
        cls.sender = FakeSender()
        cls.result = run_pipeline(
            cls.after, before=cls.before, threshold=1.0,
            sender=cls.sender, sender_address="a@x", recipient="b@x",
        )

    def test_variable_edit_seeds_the_walk_and_reaches_every_member(self):
        r = self.result
        self.assertEqual(r.walk.seeds, ("barArea",))
        self.assertEqual(len(r.walk.member_ids), 10)
        for res in r.decision.member_results:
            self.assertEqual(res.hops_from_change, 1)
            self.assertIs(res.reached_via, EdgeKind.VARIABLE_REF)

    def test_before_state_was_safe_and_the_edit_escalates(self):
        r = self.result
        self.assertIs(run_fast_gate(self.before, threshold=1.0).outcome, Outcome.APPROVED)
        self.assertIs(r.outcome, Outcome.ESCALATED)
        self.assertEqual(r.decision.violating_member_ids, ["m1", "m3"])
        for res in r.decision.member_results:
            self.assertIsNotNone(res.stress_before)
            self.assertIsNotNone(res.onshape_id)   # provenance back to the CAD occurrence

    def test_ecn_names_the_change_and_the_branch(self):
        ecn = self.result.ecn
        self.assertIs(ecn.status, ECNStatus.HELD)
        self.assertEqual(ecn.change_target, "barArea")
        self.assertEqual(ecn.value_after, "0.75 in^2")
        self.assertEqual(ecn.source.branch_name, "earl-eval-test")
        self.assertIn("Variable barArea changed from 1 in^2 to 0.75 in^2", self.result.ecn_text)
        m3 = next(l for l in ecn.lines if l.member_id == "m3")
        self.assertEqual(m3.provenance_short, "1 hop via variable_ref")
        self.assertIsNotNone(self.result.delivery)


if __name__ == "__main__":
    unittest.main()
