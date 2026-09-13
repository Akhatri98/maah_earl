"""Visualiser tests: the SVG must be well-formed, complete and honest.

Uses the Sprint 0 contract fixtures (10-bar graph + the mock escalation on
m5) so this module does not depend on the solver.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import build_decision, build_graph  # noqa: E402

from earl.analysis.visualize import (  # noqa: E402
    FAIL_COLOUR,
    LABEL_OFFSET,
    NEUTRAL_COLOUR,
    PASS_COLOUR,
    _label_positions,
    render_truss_svg,
    save_truss_svg,
)
from earl.contracts import (  # noqa: E402
    Decision,
    MemberResult,
    MemberStatus,
    Outcome,
    SupportType,
)


class TestRenderTrussSvg(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()
        self.decision = build_decision(self.graph)
        self.svg = render_truss_svg(self.graph, self.decision)

    def test_starts_with_svg_tag(self):
        self.assertTrue(self.svg.startswith("<svg"))
        self.assertTrue(self.svg.rstrip().endswith("</svg>"))

    def test_is_well_formed_xml(self):
        root = ET.fromstring(self.svg)
        self.assertTrue(root.tag.endswith("svg"))

    def test_contains_every_member_and_node_id(self):
        for m in self.graph.members:
            self.assertIn(f'id="member-{m.id}"', self.svg)
        for n in self.graph.nodes:
            self.assertIn(f'id="node-{n.id}"', self.svg)

    def test_fail_colour_on_failing_member_only(self):
        self.assertIn(FAIL_COLOUR, self.svg)
        line = next(l for l in self.svg.splitlines() if 'id="member-m5"' in l)
        self.assertIn(FAIL_COLOUR, line)
        line = next(l for l in self.svg.splitlines() if 'id="member-m1"' in l)
        self.assertIn(PASS_COLOUR, line)
        self.assertNotIn(FAIL_COLOUR, line)

    def test_safety_factor_labels(self):
        self.assertIn("m5 SF=0.62", self.svg)
        self.assertIn("m1 SF=1.85", self.svg)

    def test_affected_members_are_thicker(self):
        thick = next(l for l in self.svg.splitlines() if 'id="member-m5"' in l)
        thin = next(l for l in self.svg.splitlines() if 'id="member-m6"' in l)
        w_thick = float(thick.split('stroke-width="')[1].split('"')[0])
        w_thin = float(thin.split('stroke-width="')[1].split('"')[0])
        self.assertGreater(w_thick, w_thin)

    def test_outcome_banner(self):
        self.assertIn("ESCALATED", self.svg)
        self.assertIn("1 member below threshold 1", self.svg)

    def test_legend_and_title(self):
        self.assertIn('id="legend"', self.svg)
        self.assertIn("not evaluated", self.svg)
        self.assertIn(self.graph.id, self.svg)
        custom = render_truss_svg(self.graph, self.decision, title="Custom heading")
        self.assertIn("Custom heading", custom)

    def test_load_arrows_for_decision_load_case(self):
        self.assertIn('marker-end="url(#earl-arrow)"', self.svg)
        self.assertIn("p1 445 kN", self.svg)
        self.assertIn("p2 445 kN", self.svg)

    def test_support_glyphs(self):
        self.assertIn("<polygon", self.svg)      # PIN triangles at n5/n6
        self.graph.nodes[5].support = SupportType.ROLLER_X
        svg = render_truss_svg(self.graph, self.decision)
        supports = svg.split('id="supports"')[1].split("</g>")[0]
        self.assertIn("<circle", supports)       # circle-on-line for the roller

    def test_escapes_angle_brackets_in_text(self):
        self.graph.change.description = 'Thinned m5 <script>alert("x")</script> & more'
        self.decision.change_description = self.graph.change.description
        svg = render_truss_svg(self.graph, self.decision)
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertIn("&amp; more", svg)
        ET.fromstring(svg)   # still well-formed after escaping

    def test_no_decision_draws_neutral(self):
        svg = render_truss_svg(self.graph)
        self.assertTrue(svg.startswith("<svg"))
        drawing = svg.split('id="legend"')[0]     # the legend always shows all swatches
        self.assertNotIn(FAIL_COLOUR, drawing)
        self.assertNotIn(PASS_COLOUR, drawing)
        self.assertIn(NEUTRAL_COLOUR, drawing)
        self.assertNotIn("SF=", svg)
        for m in self.graph.members:
            self.assertIn(f'id="member-{m.id}"', svg)
        ET.fromstring(svg)

    def test_y_axis_points_up(self):
        """n5 (y = 360 in) must be drawn ABOVE n6 (y = 0), i.e. smaller canvas y."""
        def cy(node_id):
            line = next(l for l in self.svg.splitlines() if f'id="node-{node_id}"' in l)
            return float(line.split('cy="')[1].split('"')[0])
        self.assertLess(cy("n5"), cy("n6"))

    def test_auto_fits_within_canvas(self):
        for w, h in ((900, 520), (400, 300), (1600, 400)):
            svg = render_truss_svg(self.graph, self.decision, width=w, height=h)
            root = ET.fromstring(svg)
            for el in root.iter():
                if el.tag.endswith("circle") and (el.get("id") or "").startswith("node-"):
                    cx, cy = float(el.get("cx")), float(el.get("cy"))
                    self.assertTrue(0 <= cx <= w, (w, h, cx))
                    self.assertTrue(0 <= cy <= h, (w, h, cy))

    def test_error_decision_banner(self):
        d = Decision(
            id="dec-err", graph_id=self.graph.id, change_id=self.graph.change.id,
            outcome=Outcome.ERROR, error_message="SolverError: structure is unstable",
            member_results=[MemberResult(m.id, MemberStatus.NOT_EVALUATED) for m in self.graph.members],
        )
        svg = render_truss_svg(self.graph, d)
        self.assertIn("ERROR", svg)
        self.assertIn("unstable", svg)
        self.assertIn("m5 SF=n/a", svg)

    def test_approved_decision_banner(self):
        d = build_decision(self.graph)
        for r in d.member_results:
            r.safety_factor, r.status = 2.0, MemberStatus.PASS
        d.violating_member_ids, d.outcome = [], Outcome.APPROVED
        d.validate()
        svg = render_truss_svg(self.graph, d)
        self.assertIn("APPROVED", svg)
        self.assertNotIn(FAIL_COLOUR, svg.split('id="legend"')[0])


def _member_line(svg: str, member_id: str) -> tuple[float, float, float, float]:
    line = next(l for l in svg.splitlines() if f'id="member-{member_id}"' in l)
    return tuple(float(line.split(f'{k}="')[1].split('"')[0]) for k in ("x1", "y1", "x2", "y2"))


def _label_anchor(svg: str, member_id: str) -> tuple[float, float]:
    """(x, y) of the <text> whose content is the member label ('m7' or
    'm7 SF=...'). The y attribute carries a +4 baseline nudge, which is
    removed so callers get the geometric anchor."""
    root = ET.fromstring(svg)
    for el in root.iter():
        if el.tag.endswith("text") and el.text and (el.text == member_id or el.text.startswith(member_id + " ")):
            return float(el.get("x")), float(el.get("y")) - 4.0
    raise AssertionError(f"no label for {member_id}")


def _distance_to_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


class TestMemberLabelPlacement(unittest.TestCase):
    """The crossing diagonals of each bay (m7/m8, m9/m10) share a midpoint,
    so midpoint labels would overprint; and a label must not sit on its own
    line."""

    def setUp(self):
        self.graph = build_graph()
        self.decision = build_decision(self.graph)
        self.svg = render_truss_svg(self.graph, self.decision)

    def test_crossing_diagonal_labels_are_separated(self):
        for a, b in (("m7", "m8"), ("m9", "m10")):
            ax, ay = _label_anchor(self.svg, a)
            bx, by = _label_anchor(self.svg, b)
            self.assertGreater(math.hypot(ax - bx, ay - by), 20.0, (a, b))

    def test_crossing_diagonals_use_30_and_70_percent_in_member_order(self):
        x1, y1, x2, y2 = _member_line(self.svg, "m7")
        lx, ly = _label_anchor(self.svg, "m7")
        # Project the anchor back onto the member: it should be at t = 0.3.
        dx, dy = x2 - x1, y2 - y1
        t = ((lx - x1) * dx + (ly - y1) * dy) / (dx * dx + dy * dy)
        self.assertAlmostEqual(t, 0.3, places=2)
        x1, y1, x2, y2 = _member_line(self.svg, "m8")
        lx, ly = _label_anchor(self.svg, "m8")
        dx, dy = x2 - x1, y2 - y1
        t = ((lx - x1) * dx + (ly - y1) * dy) / (dx * dx + dy * dy)
        self.assertAlmostEqual(t, 0.7, places=2)

    def test_non_crossing_member_label_stays_at_midpoint(self):
        x1, y1, x2, y2 = _member_line(self.svg, "m1")   # horizontal chord
        lx, _ = _label_anchor(self.svg, "m1")
        self.assertAlmostEqual(lx, (x1 + x2) / 2, places=1)

    def test_every_label_is_offset_from_its_own_line(self):
        for m in self.graph.members:
            seg = _member_line(self.svg, m.id)
            lx, ly = _label_anchor(self.svg, m.id)
            d = _distance_to_segment(lx, ly, *seg)
            self.assertAlmostEqual(d, LABEL_OFFSET, delta=0.2, msg=m.id)

    def test_placement_holds_without_a_decision(self):
        svg = render_truss_svg(self.graph)
        ax, ay = _label_anchor(svg, "m7")
        bx, by = _label_anchor(svg, "m8")
        self.assertGreater(math.hypot(ax - bx, ay - by), 20.0)

    def test_placement_is_deterministic(self):
        self.assertEqual(self.svg, render_truss_svg(self.graph, self.decision))

    def test_label_positions_helper(self):
        # Two members sharing a midpoint -> 0.3 / 0.7 in input order; a third
        # elsewhere stays at 0.5; midpoints 1 px apart still count as shared.
        segs = [(0, 0, 100, 100), (0, 100, 100, 0), (200, 0, 300, 0), (0, 1, 100, 101)]
        fr = _label_positions(segs)
        self.assertAlmostEqual(fr[0], 0.3)
        self.assertAlmostEqual(fr[1], 0.5)
        self.assertAlmostEqual(fr[2], 0.5)
        self.assertAlmostEqual(fr[3], 0.7)
        self.assertEqual(_label_positions([]), [])
        self.assertEqual(_label_positions([(0, 0, 10, 0)]), [0.5])


class TestSaveTrussSvg(unittest.TestCase):
    def test_writes_file_and_returns_path(self):
        graph = build_graph()
        decision = build_decision(graph)
        with tempfile.TemporaryDirectory() as tmp:
            out = save_truss_svg(graph, decision, Path(tmp) / "nested" / "truss.svg")
            self.assertIsInstance(out, Path)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("<svg"))
            self.assertIn("m5 SF=0.62", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
