"""Engineering Change Notice generated from a validated Decision alone."""

from __future__ import annotations

from html import escape
from earl.contracts import Decision, Outcome


def text(decision: Decision) -> str:
    decision.validate()
    governing = decision.governing_member
    evidence = "No completed member evaluation."
    if governing:
        threshold = decision.hard_floor if governing.safety_factor < decision.hard_floor else decision.threshold
        evidence = (f"Governing member {governing.member_id}: safety factor {governing.safety_factor:.4f}; "
                    f"hard floor {decision.hard_floor:g}; design target {decision.design_target:g}. "
                    f"Compared against {threshold:g}. Capacity mode: {governing.capacity_mode}.")
    report = decision.skyciv_report
    reference = (f"Report reference: {report.report_id}. Provenance: {report.provenance}. "
                 f"{report.note or ''}" if report else "No external report is attached.")
    action = {
        Outcome.APPROVED: "The modeled change passes this structural acceptance policy. No Onshape merge or write was performed.",
        Outcome.ESCALATED: "HOLD. Notify the responsible engineer. This change is not auto-approved. Correct the change and rerun.",
        Outcome.ERROR: "HOLD. Analysis failed; no safety finding is made. Resolve the analysis error and rerun.",
    }[decision.outcome]
    reason = decision.escalation_reason.value if decision.escalation_reason else decision.error_message or "all evaluated members meet the target"
    edit = "Requested engineering edit: " + decision.change_description
    if decision.narrative and decision.narrative_provenance.startswith("provider"):
        edit += "\nNon-authoritative change summary: " + decision.narrative
    return "\n\n".join([
        f"ENGINEERING CHANGE NOTICE | {decision.id}\n{decision.outcome.value.upper()}",
        edit, evidence, f"Reason: {reason}",
        f"Reported below target: {', '.join(decision.violating_member_ids) or 'none'}.",
        action, reference,
        f"Independent cross-check: {'performed' if decision.cross_check.performed else 'NOT PERFORMED'}. {decision.cross_check.note or ''}",
        f"Evaluated: {decision.evaluated_at or 'not recorded'}. Solver: {decision.solver} {decision.solver_version or 'unavailable'}. Load case: {decision.load_case_id or 'not selected'}. Source: {decision.source_provenance}.",
        "Basis: linear elastic, axial-only, pin-jointed model; yield and ideal Euler buckling with K=1. Not an AISC/ASCE code-compliance assessment.",
        "Onshape Simulation already provides assembly analysis, stresses, displacements, and safety factors. SkyCiv supplies analysis and reports. EARL adds enforcement, notification, and the per-change decision record, not better physics.",
    ])


def render(decision: Decision) -> str:
    decision.validate()
    body = text(decision)
    sections = body.split("\n\n")
    content = "".join(f"<p>{escape(section).replace(chr(10), '<br>')}</p>" for section in sections[1:])
    report_link = f'<a href="/api/run/{escape(decision.id, quote=True)}/report" target="_blank" rel="noopener">Report reference and provenance</a>' if decision.skyciv_report else ""
    tone = {Outcome.APPROVED: "#167b56", Outcome.ESCALATED: "#b42e36", Outcome.ERROR: "#78561a"}[decision.outcome]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ECN {escape(decision.id)}</title><style>*{{box-sizing:border-box}}body{{font:13px/1.55 system-ui,sans-serif;color:#272e30;margin:0;padding:20px;background:#fff}}header{{border-bottom:2px solid {tone};padding-bottom:12px;margin-bottom:16px}}h1{{font-size:18px;line-height:1.2;margin:8px 0;color:{tone}}}p{{overflow-wrap:anywhere;margin:0 0 14px}}small{{font-size:10px;color:#697176}}a{{color:#176d55}}.id{{font-family:monospace;overflow-wrap:anywhere}}footer{{border-top:1px solid #ddd;padding-top:12px}}</style></head>
<body><header><small>EARL / ENGINEERING CHANGE NOTICE</small><h1>{decision.outcome.value.upper()}</h1><span class="id">{escape(decision.id)}</span></header>{content}<footer>{report_link}<p><small>Model capabilities: interpret edit, choose load case, draft neutral prose. No merge, write, or send tool exists for the model.</small></p></footer></body></html>"""
