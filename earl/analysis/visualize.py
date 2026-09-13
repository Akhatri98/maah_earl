"""Demo picture: a DependencyGraph (+ Decision) as a standalone SVG. [Track B, Sprint 5B]

Why stdlib-only SVG rather than matplotlib: the picture ships inside an ECN
e-mail and a scoreboard page, so it must render anywhere without a plotting
stack, and a text format is easy to assert on in tests (every member id
present, the FAIL colour present for the failing member, "<" escaped).

What the picture says, and why:

  * Members are coloured by MemberStatus (FAIL red, PASS green, grey when
    not evaluated or when there is no decision) and labelled with their
    safety factor, so a reader sees the computed number, not a verdict.
  * Affected members (graph.affected_member_ids) are drawn thicker: that is
    the graph walker's claim about what the change reaches, and the point of
    the demo is to watch a FAIL land on a member other than the one edited.
  * Supports and load arrows are drawn so a structural engineer can sanity-
    check the model at a glance (wrong support = wrong forces).
  * The outcome banner restates the Decision in words with the threshold,
    so the picture can never read as more approving than the object it
    depicts.

Geometry: the x/y plane, y axis up, auto-fitted into the canvas with
padding. z is ignored (the demo structures are planar).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from ..contracts import (
    Decision,
    DependencyGraph,
    LoadCase,
    MemberResult,
    MemberStatus,
    Outcome,
    SupportType,
)

FAIL_COLOUR = "#d33"
PASS_COLOUR = "#2a9d4a"
NEUTRAL_COLOUR = "#888"
LOAD_COLOUR = "#1f5fbf"
SUPPORT_COLOUR = "#333"
TEXT_COLOUR = "#222"
BACKGROUND_COLOUR = "#fff"
BANNER_COLOURS = {
    Outcome.APPROVED: PASS_COLOUR,
    Outcome.ESCALATED: FAIL_COLOUR,
    Outcome.ERROR: "#b36b00",
}

MEMBER_WIDTH = 3.0
AFFECTED_WIDTH = 6.5
NODE_RADIUS = 5.0
ARROW_LENGTH = 42.0
LABEL_OFFSET = 10.0        # px, perpendicular to the member, so text never sits on the line
LABEL_COINCIDENT_PX = 1.0  # midpoints closer than this are "the same point"
FONT = "font-family=\"Helvetica, Arial, sans-serif\""

# Reserved bands (px) around the structure.
TITLE_BAND = 44.0
BANNER_BAND = 34.0
LEGEND_BAND = 40.0
SIDE_PAD = 60.0
INNER_PAD = 30.0


def esc(text: object) -> str:
    """Escape text for an SVG text node or attribute (also quotes)."""
    return _xml_escape(str(text), {'"': "&quot;", "'": "&#39;"})


def status_colour(result: MemberResult | None) -> str:
    if result is None:
        return NEUTRAL_COLOUR
    if result.status is MemberStatus.FAIL:
        return FAIL_COLOUR
    if result.status is MemberStatus.PASS:
        return PASS_COLOUR
    return NEUTRAL_COLOUR


def format_sf(sf: float | None) -> str:
    if sf is None:
        return "n/a"
    if not math.isfinite(sf):
        return "inf"
    if sf >= 1.0e5:
        return f"{sf:.1e}"
    if sf >= 100:
        return f"{sf:.0f}"
    return f"{sf:.2f}"


def _format_force(newtons: float) -> str:
    mag = abs(newtons)
    if mag >= 1e6:
        return f"{mag / 1e6:.3g} MN"
    if mag >= 1e3:
        return f"{mag / 1e3:.3g} kN"
    return f"{mag:.3g} N"


# --------------------------------------------------------------------------
# Auto-fit
# --------------------------------------------------------------------------

@dataclass
class _Fit:
    scale: float
    x0: float     # world x that maps to the left edge of the drawing area
    y1: float     # world y that maps to the top edge (y axis up)
    left: float
    top: float

    def to_canvas(self, x: float, y: float) -> tuple[float, float]:
        return (self.left + (x - self.x0) * self.scale,
                self.top + (self.y1 - y) * self.scale)


def _fit(graph: DependencyGraph, width: float, height: float) -> _Fit:
    xs = [n.x for n in graph.nodes] or [0.0]
    ys = [n.y for n in graph.nodes] or [0.0]
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    dx, dy = xmax - xmin, ymax - ymin

    area_left = SIDE_PAD
    area_top = TITLE_BAND + BANNER_BAND + INNER_PAD
    area_w = max(width - 2 * SIDE_PAD, 1.0)
    area_h = max(height - area_top - LEGEND_BAND - INNER_PAD, 1.0)

    sx = area_w / dx if dx > 0 else math.inf
    sy = area_h / dy if dy > 0 else math.inf
    scale = min(sx, sy)
    if not math.isfinite(scale):        # a single node, or all collinear on one axis
        scale = 1.0
    # Centre the structure inside the drawing area.
    used_w, used_h = dx * scale, dy * scale
    left = area_left + (area_w - used_w) / 2
    top = area_top + (area_h - used_h) / 2
    return _Fit(scale=scale, x0=xmin, y1=ymax, left=left, top=top)


# --------------------------------------------------------------------------
# Pieces
# --------------------------------------------------------------------------

def _label_positions(segments: list[tuple[float, float, float, float]]) -> list[float]:
    """Fraction along each member (0..1) at which to anchor its label.

    A label sits at the midpoint unless another member's midpoint coincides
    with it (within LABEL_COINCIDENT_PX). That is exactly the crossing
    diagonals of a truss bay (m7/m8, m9/m10 on the 10-bar): both midpoints
    are the bay centre, so both labels would print on top of each other and
    the reader could not tell which SF belongs to which diagonal. Members
    sharing a midpoint are spread along their own length -- two of them land
    at 30 % and 70 % -- in member order, so the picture is deterministic.
    """
    mids = [((x1 + x2) / 2, (y1 + y2) / 2) for x1, y1, x2, y2 in segments]
    # Group indices by coincident midpoint, first-seen order.
    groups: list[list[int]] = []
    for i, (mx, my) in enumerate(mids):
        for grp in groups:
            gx, gy = mids[grp[0]]
            if math.hypot(mx - gx, my - gy) <= LABEL_COINCIDENT_PX:
                grp.append(i)
                break
        else:
            groups.append([i])
    fractions = [0.5] * len(segments)
    for grp in groups:
        k = len(grp)
        if k == 1:
            continue
        for slot, i in enumerate(grp):
            fractions[i] = 0.3 + 0.4 * slot / (k - 1)   # k == 2 -> 0.3, 0.7
    return fractions


def _support_glyph(kind: SupportType, cx: float, cy: float) -> list[str]:
    s = 11.0
    out: list[str] = []
    stroke = f'stroke="{SUPPORT_COLOUR}" stroke-width="1.5"'
    if kind in (SupportType.PIN, SupportType.FIXED):
        fill = SUPPORT_COLOUR if kind is SupportType.FIXED else "none"
        pts = f"{cx:.1f},{cy + NODE_RADIUS:.1f} {cx - s:.1f},{cy + NODE_RADIUS + 1.6 * s:.1f} {cx + s:.1f},{cy + NODE_RADIUS + 1.6 * s:.1f}"
        out.append(f'<polygon points="{pts}" fill="{fill}" {stroke}/>')
        gy = cy + NODE_RADIUS + 1.6 * s
        out.append(f'<line x1="{cx - 1.4 * s:.1f}" y1="{gy + 3:.1f}" x2="{cx + 1.4 * s:.1f}" y2="{gy + 3:.1f}" {stroke}/>')
    elif kind is SupportType.ROLLER_X:
        # circle under the node on a horizontal line: free to roll along X
        cy2 = cy + NODE_RADIUS + s * 0.8
        out.append(f'<circle cx="{cx:.1f}" cy="{cy2:.1f}" r="{s * 0.7:.1f}" fill="none" {stroke}/>')
        gy = cy2 + s * 0.7 + 2
        out.append(f'<line x1="{cx - 1.4 * s:.1f}" y1="{gy:.1f}" x2="{cx + 1.4 * s:.1f}" y2="{gy:.1f}" {stroke}/>')
    elif kind is SupportType.ROLLER_Y:
        # circle beside the node on a vertical line: free to roll along Y
        cx2 = cx - NODE_RADIUS - s * 0.8
        out.append(f'<circle cx="{cx2:.1f}" cy="{cy:.1f}" r="{s * 0.7:.1f}" fill="none" {stroke}/>')
        gx = cx2 - s * 0.7 - 2
        out.append(f'<line x1="{gx:.1f}" y1="{cy - 1.4 * s:.1f}" x2="{gx:.1f}" y2="{cy + 1.4 * s:.1f}" {stroke}/>')
    return out


def _choose_load_case(graph: DependencyGraph, decision: Decision | None) -> LoadCase | None:
    if decision is not None and decision.load_case_id:
        for lc in graph.load_cases:
            if lc.id == decision.load_case_id:
                return lc
    return graph.load_cases[0] if graph.load_cases else None


def _load_arrows(graph: DependencyGraph, lc: LoadCase | None, fit: _Fit) -> list[str]:
    if lc is None:
        return []
    out: list[str] = []
    for pl in lc.point_loads:
        mag = math.hypot(pl.fx, pl.fy)
        if mag == 0:
            if pl.fz == 0:
                continue
            # Out-of-plane load: mark it rather than draw nothing.
            cx, cy = fit.to_canvas(graph.node(pl.node_id).x, graph.node(pl.node_id).y)
            out.append(
                f'<text x="{cx + 8:.1f}" y="{cy - 8:.1f}" font-size="11" fill="{LOAD_COLOUR}" {FONT}>'
                f"{esc(pl.id)} Fz={esc(_format_force(pl.fz))}</text>"
            )
            continue
        node = graph.node(pl.node_id)
        cx, cy = fit.to_canvas(node.x, node.y)
        ux, uy = pl.fx / mag, -pl.fy / mag     # canvas y is down
        # The arrow points INTO the node along the load direction.
        tx, ty = cx - ux * NODE_RADIUS, cy - uy * NODE_RADIUS
        sx, sy = tx - ux * ARROW_LENGTH, ty - uy * ARROW_LENGTH
        out.append(
            f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{tx:.1f}" y2="{ty:.1f}" '
            f'stroke="{LOAD_COLOUR}" stroke-width="2.5" marker-end="url(#earl-arrow)"/>'
        )
        label = f"{pl.id} {_format_force(mag)}"
        out.append(
            f'<text x="{sx + 6:.1f}" y="{sy - 4:.1f}" font-size="11" fill="{LOAD_COLOUR}" {FONT}>'
            f"{esc(label)}</text>"
        )
    return out


def _banner_text(decision: Decision | None, graph: DependencyGraph) -> str:
    if decision is None:
        return f"no decision — {len(graph.members)} members drawn, none evaluated"
    thr = decision.safety_factor_threshold
    if decision.outcome is Outcome.ESCALATED:
        n = len(decision.violating_member_ids)
        return f"ESCALATED — {n} member{'s' if n != 1 else ''} below threshold {thr:g}"
    if decision.outcome is Outcome.APPROVED:
        evaluated = sum(1 for r in decision.member_results if r.safety_factor is not None)
        return f"APPROVED — all {evaluated} evaluated members at or above threshold {thr:g}"
    return f"ERROR — {decision.error_message or 'analysis could not be completed'}"


def _legend(y: float) -> list[str]:
    items = [
        (FAIL_COLOUR, MEMBER_WIDTH, "FAIL"),
        (PASS_COLOUR, MEMBER_WIDTH, "PASS"),
        (NEUTRAL_COLOUR, MEMBER_WIDTH, "not evaluated"),
        (NEUTRAL_COLOUR, AFFECTED_WIDTH, "affected (thick)"),
    ]
    out = ['<g id="legend">']
    x = SIDE_PAD
    for colour, w, label in items:
        out.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + 28:.1f}" y2="{y:.1f}" stroke="{colour}" stroke-width="{w}"/>')
        out.append(f'<text x="{x + 34:.1f}" y="{y + 4:.1f}" font-size="12" fill="{TEXT_COLOUR}" {FONT}>{esc(label)}</text>')
        x += 34 + 8 * len(label) + 28
    out.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x + 28:.1f}" y2="{y:.1f}" stroke="{LOAD_COLOUR}" stroke-width="2.5" marker-end="url(#earl-arrow)"/>')
    out.append(f'<text x="{x + 34:.1f}" y="{y + 4:.1f}" font-size="12" fill="{TEXT_COLOUR}" {FONT}>point load</text>')
    out.append("</g>")
    return out


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def render_truss_svg(
    graph: DependencyGraph,
    decision: Decision | None = None,
    *,
    width: int = 900,
    height: int = 520,
    title: str | None = None,
) -> str:
    """Render the structure (and, if given, the verdict on it) as SVG text.
    Returns a standalone document starting with `<svg`."""
    fit = _fit(graph, float(width), float(height))
    results: dict[str, MemberResult] = (
        {r.member_id: r for r in decision.member_results} if decision else {}
    )
    affected = set(graph.affected_member_ids)
    focus_target = graph.change.target_id

    if title is None:
        title = f"{graph.id}: {graph.change.description}"

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">',
        "<defs>",
        '<marker id="earl-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">',
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{LOAD_COLOUR}"/>',
        "</marker>",
        "</defs>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{BACKGROUND_COLOUR}"/>',
        f'<text x="{SIDE_PAD:.1f}" y="28" font-size="17" font-weight="bold" fill="{TEXT_COLOUR}" {FONT}>{esc(title)}</text>',
    ]

    # Outcome banner.
    banner_colour = BANNER_COLOURS.get(decision.outcome, NEUTRAL_COLOUR) if decision else NEUTRAL_COLOUR
    by = TITLE_BAND
    parts.append(f'<rect x="{SIDE_PAD:.1f}" y="{by:.1f}" width="{width - 2 * SIDE_PAD:.1f}" height="{BANNER_BAND - 8:.1f}" rx="4" fill="{banner_colour}" fill-opacity="0.12" stroke="{banner_colour}"/>')
    parts.append(f'<text x="{SIDE_PAD + 10:.1f}" y="{by + 18:.1f}" font-size="13" font-weight="bold" fill="{banner_colour}" {FONT}>{esc(_banner_text(decision, graph))}</text>')

    # Members (drawn first so nodes sit on top). Affected members are thicker;
    # the change target gets a dashed outline so the edited member is
    # distinguishable from the ones it dragged along.
    parts.append('<g id="members">')
    segments = [
        fit.to_canvas(graph.node(m.start_node).x, graph.node(m.start_node).y)
        + fit.to_canvas(graph.node(m.end_node).x, graph.node(m.end_node).y)
        for m in graph.members
    ]
    fractions = _label_positions(segments)
    for m, (x1, y1, x2, y2), t in zip(graph.members, segments, fractions):
        res = results.get(m.id)
        colour = status_colour(res)
        w = AFFECTED_WIDTH if m.id in affected else MEMBER_WIDTH
        dash = ' stroke-dasharray="9,5"' if m.id == focus_target else ""
        parts.append(
            f'<line id="member-{esc(m.id)}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{colour}" stroke-width="{w}" stroke-linecap="round"{dash}/>'
        )
        # Label at fraction t along the member (0.5 unless another member's
        # midpoint coincides, see _label_positions), pushed off the line
        # along its normal so the text never overprints the stroke.
        px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        length = math.hypot(x2 - x1, y2 - y1) or 1.0
        nx, ny = -(y2 - y1) / length, (x2 - x1) / length
        lx, ly = px + nx * LABEL_OFFSET, py + ny * LABEL_OFFSET
        if decision is not None:
            label = f"{m.id} SF={format_sf(res.safety_factor) if res else 'n/a'}"
        else:
            label = m.id
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" font-size="12" text-anchor="middle" '
            f'fill="{colour if res else TEXT_COLOUR}" {FONT}>{esc(label)}</text>'
        )
    parts.append("</g>")

    # Supports.
    parts.append('<g id="supports">')
    for n in graph.nodes:
        if n.support is SupportType.FREE:
            continue
        cx, cy = fit.to_canvas(n.x, n.y)
        parts.extend(_support_glyph(n.support, cx, cy))
    parts.append("</g>")

    # Loads.
    parts.append('<g id="loads">')
    parts.extend(_load_arrows(graph, _choose_load_case(graph, decision), fit))
    parts.append("</g>")

    # Nodes.
    parts.append('<g id="nodes">')
    for n in graph.nodes:
        cx, cy = fit.to_canvas(n.x, n.y)
        parts.append(
            f'<circle id="node-{esc(n.id)}" cx="{cx:.1f}" cy="{cy:.1f}" r="{NODE_RADIUS}" '
            f'fill="{BACKGROUND_COLOUR}" stroke="{TEXT_COLOUR}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{cx + 8:.1f}" y="{cy - 8:.1f}" font-size="12" fill="{TEXT_COLOUR}" {FONT}>{esc(n.id)}</text>'
        )
    parts.append("</g>")

    parts.extend(_legend(height - LEGEND_BAND / 2))
    parts.append("</svg>")
    return "\n".join(parts)


def save_truss_svg(graph: DependencyGraph, decision: Decision | None, path: Path | str) -> Path:
    """Write the SVG to `path` (parents created) and return it as a Path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_truss_svg(graph, decision), encoding="utf-8")
    return out


__all__ = [
    "FAIL_COLOUR",
    "PASS_COLOUR",
    "NEUTRAL_COLOUR",
    "render_truss_svg",
    "save_truss_svg",
]
