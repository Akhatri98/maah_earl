"""The demo, in order, from one command. [Sprint 6 -- both tracks]

    python3 scripts/demo.py                 # the whole flow, offline, ~4 minutes
    python3 scripts/demo.py --pause          # stop between acts (press enter)
    python3 scripts/demo.py --act 4          # just one act, for rehearsal
    python3 scripts/demo.py --skyciv --send  # live Stage 3 + Stage 4

Everything here is offline by default and reads the RECORDED eval run in
`demo/recordings`, so the demo cannot fail on a network hiccup, a rotated key
or a rate limit. The numbers on screen are the ones measured live in Sprint
5A; `scripts/run_eval.py --replay demo/recordings` reproduces them exactly.

## What changed after Sprint 5A measured the thing

plan.md's flow opened by showing a baseline LLM missing a domino the system
catches. **We ran that comparison twenty times and the baseline did not miss
anything** (earl/eval/README.md): one `solve_load_case` call returns every
member, so on a six-node truss there is nothing for a dependency walk to find
that brute force does not.

So the demo does not claim a gap that is not there. It claims the one that is:

    A capable model FOUND every unsafe member on this truss.
    It is still not ALLOWED to decide whether one ships.

Act 4 is the new centre of gravity -- the code refusing an approval that
contradicts a computed number -- and Act 5 reports the tie honestly instead of
hiding it. That is a narrower claim than the pitch started with, and it is the
one the numbers support.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.analysis.benchmark import (  # noqa: E402
    DEMO_AREA_AFTER_IN2,
    DEMO_AREA_BEFORE_IN2,
    DEMO_MEMBER,
    demo_before_graph,
    demo_change_graph,
)
from earl.analysis.scoreboard import load_records, score  # noqa: E402
from earl.artifacts.skyciv_client import SkyCivClient  # noqa: E402
from earl.config import GmailConfig, skyciv_report_dir  # noqa: E402
from earl.contracts import Outcome  # noqa: E402
from earl.delivery.gmail import GmailSender, OutboxSender  # noqa: E402
from earl.pipeline import run_pipeline  # noqa: E402

RECORDINGS = ROOT / "demo" / "recordings"
RECORDS_JSON = RECORDINGS / "records.json"
OUT = ROOT / "artifacts" / "demo"

RULE = "=" * 78


def _title(number: int, text: str) -> None:
    print()
    print(RULE)
    print(f"  ACT {number}.  {text}")
    print(RULE)
    print()


def _wait(pause: bool) -> None:
    if pause:
        try:
            input("\n        [enter]")
        except (EOFError, KeyboardInterrupt):
            pass


# --------------------------------------------------------------------------
# Acts
# --------------------------------------------------------------------------

def act1_the_change() -> None:
    """Open with the change, not the pitch."""
    _title(1, "An engineer thins one diagonal to save weight")
    graph = demo_change_graph()
    print(f"  Document : {graph.source.document_id}")
    print(f"  Branch   : {graph.source.branch_name or '(evaluation branch)'}")
    print(f"  Change   : {graph.change.description}")
    print()
    print(f"  Member {DEMO_MEMBER} goes from {DEMO_AREA_BEFORE_IN2} in^2 to "
          f"{DEMO_AREA_AFTER_IN2:.3f} in^2.")
    print("  Nothing else is touched. The engineer's question is: is that fine?")
    print()
    print("  The honest answer is that nobody can tell by looking, because a")
    print("  10-bar truss is statically indeterminate -- thinning one member")
    print("  pushes its load into members nobody edited.")


def act2_the_system(result) -> None:
    """The graph walk and the real numbers behind the verdict."""
    _title(2, "EARL walks every dependency and solves the structure")
    walk = result.walk
    decision = result.decision

    print(f"  Fixed BFS from {DEMO_MEMBER}: {len(walk.member_ids)} of "
          f"{len(result.graph.members)} members reached.")
    for distance in range(0, walk.max_distance + 1):
        ring = walk.at_distance(distance)
        members = [e for e in ring if e.startswith("m")]
        if members:
            label = "the change itself" if distance == 0 else f"{distance} hop(s) out"
            print(f"    {label:<18} {', '.join(members)}")
    print()
    print("  This is a fixed algorithm, not a model decision. It crosses every")
    print("  edge by construction, so nothing is missed by being forgotten.")
    print()
    print(f"  {'member':<8}{'stress (ksi)':>14}{'safety factor':>15}   verdict")
    for r in sorted(decision.member_results, key=lambda x: (x.safety_factor or 9e9)):
        if r.safety_factor is None:
            continue
        ksi = (r.stress_after or 0.0) / 6.894757e6
        flag = "FAIL  <-- nobody edited this" if r.status.value == "fail" else "pass"
        print(f"  {r.member_id:<8}{ksi:>14.2f}{r.safety_factor:>15.3f}   {flag}")
    print()
    print(f"  The edited member {DEMO_MEMBER} is FINE. Its neighbour "
          f"{decision.violating_member_ids[0]} is not.")
    print("  That is the dropped domino this project exists to catch.")


def act3_the_paperwork(result) -> None:
    """The ECN, the report it cites, and where it went."""
    _title(3, "A change notice an engineer would recognise")
    ecn = result.ecn
    print(f"  {ecn.id}   status: {ecn.status.headline}")
    print()
    for line in result.ecn_text.splitlines()[:26]:
        print("  " + line)
    print("  ...")
    print()
    if result.delivery is not None:
        r = result.delivery
        print(f"  Delivered via {r.channel} to {r.to}")
        print(f"  Attachments  : {', '.join(r.attachments)}")
        if r.channel == "outbox":
            print(f"  Written to   : {r.message_id}")
            print("                 (byte for byte what Gmail would receive)")


def act4_the_guarantee(result) -> str:
    """The centre of the demo: the threshold is code, not judgement."""
    _title(4, "The part a language model cannot promise")
    decision = result.decision
    worst = decision.governing_member

    print("  In Sprint 5A we ran a tool-using LLM over the same twenty")
    print("  scenarios, on the same model, with the same solver. It found")
    print("  every unsafe member. It did not miss one.")
    print()
    print("  So this is not a demo about a model being bad at arithmetic.")
    print("  It is a demo about who is ALLOWED to decide.")
    print()
    print(f"  Watch. {worst.member_id} has a safety factor of "
          f"{worst.safety_factor:.3f}, below the threshold of "
          f"{decision.safety_factor_threshold:g}.")
    print("  Suppose something upstream -- a model, a bug, a tired human --")
    print("  decides to approve it anyway:")
    print()
    print("      decision.outcome = Outcome.APPROVED")
    print("      decision.validate()")
    print()

    tampered = type(decision).from_json(decision.to_json())
    tampered.outcome = Outcome.APPROVED
    try:
        tampered.validate()
    except ValueError as e:
        print("  -> ValueError:")
        for chunk in str(e).split("; "):
            print(f"       {chunk}")
        print()
        print("  It does not warn, and it does not log and continue. It raises,")
        print("  at the boundary between the two halves of the pipeline, before")
        print("  anything merges and before any notice goes out.")
        print()
        print("  There is no flag to turn that off, no prompt that talks it")
        print("  round, and no merge method the model can reach. An LLM that")
        print("  gets the physics right every time still cannot ship this")
        print("  change, because deciding is not a thing it is wired to do.")
        return "enforced"

    print("  !! validate() did not raise -- the guarantee is broken.")
    return "BROKEN"


def act5_the_numbers() -> None:
    """The scoreboard, reported as measured."""
    _title(5, "Twenty scenarios, and what they actually showed")
    if not RECORDS_JSON.exists():
        print(f"  (no recorded run at {RECORDS_JSON})")
        print("  Run: python3 scripts/run_eval.py --agents system,baseline")
        return

    board = score(load_records(RECORDS_JSON))
    for line in board.to_markdown().splitlines():
        print("  " + line)
    print()
    print("  Read that honestly: the baseline tied us.")
    print()
    print("  Our pitch said a bare LLM would be caught 'missing something, or")
    print("  inconsistent across runs'. We tested both halves.")
    print()
    _consistency_lines()
    print()
    print("  It missed nothing, and it did not wobble. Both halves of our own")
    print("  claim came back negative, and this slide says so.")
    print()
    print("  Why -- we read the transcripts rather than guessing: one solver")
    print("  call returns all ten members, so on a truss this small 'check")
    print("  everything downstream' and 'check everything' are the same action.")
    print("  The dependency walk has nothing to find that brute force does not.")
    print("  We could have handed the baseline a one-member-at-a-time solver")
    print("  and manufactured a gap. That is the exact dishonesty this project")
    print("  exists to catch, so we did not.")
    print()
    print("  What the run does establish:")
    print("    * 22 of 22 unsafe members caught, zero false alarms, the same")
    print("      answer on all 60 runs -- ours by construction, and provably so.")
    print("    * 9 of the 20 changes were genuinely safe and were approved, so")
    print("      this is not a system that escalates everything and calls it")
    print("      caution.")
    print("    * and the guarantee in Act 4, which no score can express: the")
    print("      model matched us at finding problems and still cannot decide")
    print("      that one ships.")


def _consistency_lines() -> None:
    """The repeat run, if it was recorded. Reported whichever way it came out
    -- a stable baseline is a negative result for our pitch, and hiding it
    would be the one thing this project cannot afford to do."""
    path = RECORDINGS / "consistency.md"
    if not path.exists():
        print("  (no recorded consistency run; "
              "run: scripts/run_eval.py --agents system,baseline --repeat 3)")
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            print("  " + line)


def act6_recap(status: str, result) -> None:
    _title(6, "One line each")
    print("  Ingestion      Onshape -> dependency graph, evaluated on a branch")
    print("  Fast gate      PyNite solves, Biject applies the threshold IN CODE")
    if result.decision.skyciv_report is not None:
        print("  Trusted report SkyCiv, independently, when something is escalated")
    else:
        # Never claim a stage that did not run. SkyCiv's free tier caps a
        # model at five members and the demo truss has ten, so Stage 3 stops
        # at an account limit rather than a payload error -- the ECN in Act 3
        # says so too, rather than leaving the citation blank.
        print("  Trusted report SkyCiv -- NOT RUN here: the free tier caps a model")
        print("                 at 5 members and this truss has 10. The ECN says so")
        print("                 rather than citing evidence nobody can open.")
    print("  Sharing        an ECN that cites it, in the engineer's inbox")
    print()
    print(RULE)
    if status == "enforced":
        print("  We spent a sprint trying to prove a language model would miss")
        print("  something. Over 60 runs, it did not.")
        print()
        print("  It is still not the thing that decides whether the change ships.")
    else:
        print("  !! The enforcement check did not hold -- do not present this.")
    print(RULE)


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pause", action="store_true", help="stop between acts")
    parser.add_argument("--act", type=int, default=None, help="run one act only")
    parser.add_argument("--skyciv", action="store_true", help="live Stage 3")
    parser.add_argument("--send", action="store_true", help="live Stage 4 (Gmail)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    skyciv = SkyCivClient.from_env(record_dir=OUT / "skyciv") if args.skyciv else None
    if args.send:
        config = GmailConfig.from_env()
        sender, address, recipient = GmailSender(config), config.sender, config.recipient
    else:
        sender = OutboxSender(OUT / "outbox")
        address, recipient = "earl@localhost", "engineer@localhost"

    result = run_pipeline(
        demo_change_graph(),
        before=demo_before_graph(),
        skyciv=skyciv,
        report_dir=skyciv_report_dir() or (OUT / "reports"),
        sender=sender,
        sender_address=address,
        recipient=recipient,
    )
    result.write(OUT)

    acts = [args.act] if args.act else [1, 2, 3, 4, 5, 6]
    status = "enforced"
    for act in acts:
        if act == 1:
            act1_the_change()
        elif act == 2:
            act2_the_system(result)
        elif act == 3:
            act3_the_paperwork(result)
        elif act == 4:
            status = act4_the_guarantee(result)
        elif act == 5:
            act5_the_numbers()
        elif act == 6:
            act6_recap(status, result)
        if act != acts[-1]:
            _wait(args.pause)

    return 0 if status == "enforced" else 1


if __name__ == "__main__":
    raise SystemExit(main())
