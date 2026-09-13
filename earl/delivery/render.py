"""ECN renderers. [Track A, Sprint 3A]

Two surfaces, one document: plain text (the terminal, and later the Gmail body)
and Markdown (the repo's change record, and anything that wants structure).

Both walk the SAME fixed section list in the SAME order. A section is never
dropped for being empty -- it says "none" or "not performed" instead. That is
what "templated, not generated freeform" buys: a reader learns the shape once,
and a missing section is then a signal rather than a formatting quirk.

Output is ASCII. See the note in units.py -- the demo runs on a Windows console
and mojibake in a live demo reads as a bug in the engineering.
"""

from __future__ import annotations

from .ecn import ECN, ECNLine, ECNStatus
from . import units as fmt

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH


# --------------------------------------------------------------------------
# Shared section content
# --------------------------------------------------------------------------

def _wrap(text: str, width: int = WIDTH, indent: str = "") -> list[str]:
    """Greedy wrap. Deliberately not textwrap.fill: the ECN has strings that
    must not be broken (report ids, URLs), and this keeps an over-long token on
    its own line rather than hyphenating or truncating it."""
    out: list[str] = []
    line = indent
    for word in text.split():
        candidate = f"{line}{word}" if line == indent else f"{line} {word}"
        if len(candidate) > width and line != indent:
            out.append(line)
            line = f"{indent}{word}"
        else:
            line = candidate
    out.append(line)
    return out


def _change_rows(ecn: ECN) -> list[tuple[str, str]]:
    rows = [("Change id", ecn.change_id)]
    if ecn.change_target:
        rows.append(("Target", ecn.change_target))
    if ecn.value_before is not None or ecn.value_after is not None:
        rows.append(("Value", f"{ecn.value_before or '?'} -> {ecn.value_after or '?'}"))
    return rows


def _source_rows(ecn: ECN) -> list[tuple[str, str]]:
    if ecn.source is None:
        return []
    src = ecn.source
    rows = [("Document", src.document_id), ("Element", src.element_id)]
    if src.branch_name:
        rows.append(("Branch", f"{src.branch_name}  (evaluation sandbox, not main)"))
    if src.workspace_id:
        rows.append(("Workspace", src.workspace_id))
    if src.version_id:
        rows.append(("Version", src.version_id))
    return rows


def _governing_rows(ecn: ECN, line: ECNLine) -> list[tuple[str, str]]:
    label = "Member"
    rows = [(label, line.member_id)]
    if line.onshape_id:
        rows.append(("Onshape part", line.onshape_id))
    rows += [
        ("Safety factor", f"{fmt.ratio(line.safety_factor)}  "
                          f"(threshold {ecn.safety_factor_threshold:.2f})"),
        ("Utilization", fmt.percent(line.utilization)),
        ("Stress after", fmt.stress(line.stress_after, ecn.units)),
        ("Stress before", fmt.stress(line.stress_before, ecn.units)),
        ("Capacity", fmt.stress(line.capacity, ecn.units)),
    ]
    if line.provenance:
        rows.append(("Reached by", line.provenance))
    return rows


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------

def _kv_block(rows: list[tuple[str, str]], indent: str = "  ") -> list[str]:
    if not rows:
        return []
    pad = max(len(k) for k, _ in rows)
    out: list[str] = []
    for key, value in rows:
        prefix = f"{indent}{key.ljust(pad)}  "
        wrapped = _wrap(value, width=WIDTH, indent=" " * len(prefix))
        out.append(prefix + wrapped[0].lstrip())
        out.extend(wrapped[1:])
    return out


def _text_table(ecn: ECN) -> list[str]:
    header = ("Member", "Status", "Safety", "Util", "Reached by")
    rows: list[tuple[str, ...]] = []
    for line in ecn.lines:
        marker = "*" if line.failed else " "
        member = f"{marker}{line.member_id}"
        if line.is_change_target:
            member += " (target)"
        rows.append((
            member,
            line.status.value.upper() if line.failed else line.status.value,
            fmt.ratio(line.safety_factor),
            fmt.percent(line.utilization),
            line.provenance_short or line.provenance or "-",
        ))

    widths = [
        max(len(header[i]), max((len(r[i]) for r in rows), default=0))
        for i in range(5)
    ]
    # The provenance column absorbs whatever is left rather than pushing the
    # table past the page width.
    widths[4] = min(widths[4], max(10, WIDTH - sum(widths[:4]) - 8))

    def fmt_row(cells: tuple[str, ...]) -> str:
        out = "  ".join(c.ljust(w)[:w] for c, w in zip(cells, widths))
        return out.rstrip()

    lines = [fmt_row(header), "  ".join("-" * w for w in widths)]
    lines += [fmt_row(r) for r in rows]
    return lines


def render_text(ecn: ECN) -> str:
    """The ECN as plain text."""
    out: list[str] = [
        RULE,
        "ENGINEERING CHANGE NOTICE".center(WIDTH),
        RULE,
        f"  {ecn.id}",
        "",
    ]
    out += _kv_block([
        ("Status", ecn.status.headline),
        ("Raised", ecn.raised_at),
        ("Originator", ecn.originator),
        ("Decision", ecn.decision_id),
    ])
    out += ["", THIN, "1. CHANGE", THIN, ""]
    out += _wrap(ecn.change_description or "(no description supplied)", indent="  ")
    out += [""] + _kv_block(_change_rows(ecn))

    source_rows = _source_rows(ecn)
    out += ["", THIN, "2. SOURCE", THIN, ""]
    out += _kv_block(source_rows) if source_rows else _wrap(
        "No Onshape source reference was carried on the decision.", indent="  ")

    out += ["", THIN, "3. DISPOSITION", THIN, ""]
    out += _wrap(ecn.disposition, indent="  ")

    out += ["", THIN, "4. GOVERNING MEMBER", THIN, ""]
    governing = ecn.governing
    if governing is None:
        out += _wrap(
            "No member carried a computed safety factor, so no governing "
            "member could be identified.", indent="  ")
    else:
        out += _kv_block(_governing_rows(ecn, governing))

    out += ["", THIN,
            f"5. AFFECTED ITEMS  ({len(ecn.lines)} evaluated, "
            f"{len(ecn.failed_lines)} below threshold)",
            THIN, ""]
    if ecn.lines:
        out += ["  " + line for line in _text_table(ecn)]
        # Only explain the marker when one is actually on the page; a legend
        # for a symbol that appears nowhere reads as a missing row.
        if ecn.failed_lines:
            out += ["", "  * marks a member below the safety-factor threshold."]
    else:
        out += _wrap("No members were evaluated.", indent="  ")

    out += ["", THIN, "6. SUPPORTING EVIDENCE", THIN, ""]
    for item in ecn.evidence:
        out += _wrap(item, indent="  ")

    out += ["", THIN, "7. INDEPENDENT CROSS-CHECK", THIN, ""]
    out += _wrap(ecn.cross_check_note, indent="  ")

    out += ["", THIN, "8. SOLVER SANITY CHECKS", THIN, ""]
    for part in ecn.sanity_note.split("; "):
        out += _wrap(part, indent="  ")
    if ecn.solver:
        out += [""] + _kv_block([("Solver", ecn.solver)])

    if ecn.notes:
        out += ["", THIN, "9. NOTES ON THIS NOTICE", THIN, ""]
        for note in ecn.notes:
            out += _wrap(f"- {note}", indent="  ")

    out += ["", RULE]
    if ecn.requires_signature:
        out += [
            "  ENGINEERING DISPOSITION REQUIRED",
            "",
            "    [ ] Approve as-is    [ ] Approve with conditions    [ ] Reject",
            "",
            "    Engineer: ______________________   Date: ______________",
            "",
            "  This change does not merge until this notice is signed.",
        ]
    else:
        out += [
            "  No signature required. Auto-approved under the threshold rule and",
            "  logged as the change record.",
        ]
    out += [RULE, ""]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def _md_table(ecn: ECN) -> list[str]:
    out = [
        "| Member | Status | Safety factor | Utilization | Reached by |",
        "|---|---|---:|---:|---|",
    ]
    for line in ecn.lines:
        member = f"`{line.member_id}`"
        if line.is_change_target:
            member += " _(target)_"
        status = f"**{line.status.value.upper()}**" if line.failed else line.status.value
        out.append(
            f"| {member} | {status} | {fmt.ratio(line.safety_factor)} | "
            f"{fmt.percent(line.utilization)} | "
            f"{line.provenance_short or line.provenance or '-'} |"
        )
    return out


def _md_kv(rows: list[tuple[str, str]]) -> list[str]:
    return [f"- **{k}:** {v}" for k, v in rows]


def render_markdown(ecn: ECN) -> str:
    """The ECN as Markdown, following the same section order as the text form."""
    out = [
        f"# Engineering Change Notice {ecn.id}",
        "",
        f"> **{ecn.status.headline}**",
        "",
    ]
    out += _md_kv([
        ("Raised", ecn.raised_at),
        ("Originator", ecn.originator),
        ("Decision", f"`{ecn.decision_id}`"),
    ])

    out += ["", "## 1. Change", "", ecn.change_description or "_(no description supplied)_", ""]
    out += _md_kv(_change_rows(ecn))

    out += ["", "## 2. Source", ""]
    source_rows = _source_rows(ecn)
    out += _md_kv(source_rows) if source_rows else [
        "_No Onshape source reference was carried on the decision._"]

    out += ["", "## 3. Disposition", "", ecn.disposition]

    out += ["", "## 4. Governing member", ""]
    governing = ecn.governing
    if governing is None:
        out += ["_No member carried a computed safety factor._"]
    else:
        out += _md_kv(_governing_rows(ecn, governing))

    out += ["",
            f"## 5. Affected items ({len(ecn.lines)} evaluated, "
            f"{len(ecn.failed_lines)} below threshold)",
            ""]
    out += _md_table(ecn) if ecn.lines else ["_No members were evaluated._"]

    out += ["", "## 6. Supporting evidence", ""]
    out += [f"- {item}" for item in ecn.evidence]

    out += ["", "## 7. Independent cross-check", "", ecn.cross_check_note]

    out += ["", "## 8. Solver sanity checks", ""]
    out += [f"- {part}" for part in ecn.sanity_note.split("; ")]
    if ecn.solver:
        out += [f"- Solver: {ecn.solver}"]

    if ecn.notes:
        out += ["", "## 9. Notes on this notice", ""]
        out += [f"- {note}" for note in ecn.notes]

    out += ["", "---", ""]
    if ecn.requires_signature:
        out += [
            "**Engineering disposition required**",
            "",
            "- [ ] Approve as-is",
            "- [ ] Approve with conditions",
            "- [ ] Reject",
            "",
            "Engineer: ______________________  Date: ______________",
            "",
            "_This change does not merge until this notice is signed._",
        ]
    else:
        out += [
            "_No signature required. Auto-approved under the threshold rule and "
            "logged as the change record._"
        ]
    return "\n".join(out) + "\n"
