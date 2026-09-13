"""Sprint 2A tests: change detection and the branch evaluation sandbox.

The branch tests drive a fake transport rather than the live API. That keeps
the suite free and offline, and -- more to the point -- a live branch test
would create a permanent Onshape version on every run, since Onshape cannot
delete versions. The live cycle is proven separately and deliberately by
`scripts/onshape_branch_smoketest.py`.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earl.config import OnshapeConfig  # noqa: E402
from earl.contracts.graph import ChangeKind  # noqa: E402
from earl.ingestion.branch import BranchSession, evaluate_in_branch  # noqa: E402
from earl.ingestion.changes import (  # noqa: E402
    change_id,
    detect_changes,
    detect_variable_changes,
    undriven_changes,
)
from earl.ingestion.onshape_client import (  # noqa: E402
    OnshapeClient,
    OnshapeError,
    RequestRecord,
)
from earl.ingestion.parsers import (  # noqa: E402
    Variable,
    feature_with_expression,
    parse_variable_features,
)

PARTSTUDIO = "1a595ebc191cbaa887523bc7"


def var(name: str, expression: str, type_: str = "LENGTH") -> Variable:
    return Variable(name=name, type=type_, expression=expression)


BASE_TABLE = [
    var("bayWidth", "360 in"),
    var("bayHeight", "360 in"),
    var("barArea", "1 in^2", "ANY"),
]


# --------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------

class TestChangeDetection(unittest.TestCase):
    def test_edited_variable_becomes_a_variable_edit(self):
        after = [var("bayWidth", "360 in"), var("bayHeight", "360 in"),
                 var("barArea", "0.4 in^2", "ANY")]
        events = detect_variable_changes(BASE_TABLE, after)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIs(event.kind, ChangeKind.VARIABLE_EDIT)
        self.assertEqual(event.target_id, "barArea")
        self.assertEqual(event.value_before, "1 in^2")
        self.assertEqual(event.value_after, "0.4 in^2")

    def test_description_is_templated_and_readable(self):
        """It flows verbatim into the ECN, so it has to read like a sentence."""
        after = [*BASE_TABLE[:2], var("barArea", "0.4 in^2", "ANY")]
        description = detect_variable_changes(BASE_TABLE, after)[0].description
        self.assertEqual(
            description, "Variable barArea changed from 1 in^2 to 0.4 in^2"
        )

    def test_added_variable_is_a_change(self):
        after = [*BASE_TABLE, var("newVar", "5 in")]
        events = detect_variable_changes(BASE_TABLE, after)
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].value_before)
        self.assertIn("added", events[0].description)

    def test_removed_variable_is_a_change_not_a_skipped_row(self):
        """Removing the variable a feature reads changes the geometry too."""
        events = detect_variable_changes(BASE_TABLE, BASE_TABLE[:2])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].target_id, "barArea")
        self.assertIsNone(events[0].value_after)
        self.assertIn("removed", events[0].description)

    def test_no_difference_yields_no_events(self):
        self.assertEqual(detect_variable_changes(BASE_TABLE, BASE_TABLE), [])

    def test_every_changed_variable_is_reported(self):
        """One graph carries one change, so dropping the second would mean a
        real edit never gets analysed."""
        after = [var("bayWidth", "400 in"), var("bayHeight", "360 in"),
                 var("barArea", "0.4 in^2", "ANY")]
        events = detect_variable_changes(BASE_TABLE, after)
        self.assertEqual(
            sorted(e.target_id for e in events), ["barArea", "bayWidth"]
        )

    def test_ids_are_stable_across_runs(self):
        """Replaying a scenario must not look like a different change."""
        after = [*BASE_TABLE[:2], var("barArea", "0.4 in^2", "ANY")]
        first = detect_variable_changes(BASE_TABLE, after)[0].id
        second = detect_variable_changes(BASE_TABLE, after)[0].id
        self.assertEqual(first, second)

    def test_different_changes_get_different_ids(self):
        self.assertNotEqual(
            change_id("variable", "barArea", "1 in^2", "0.4 in^2"),
            change_id("variable", "barArea", "1 in^2", "0.5 in^2"),
        )

    def test_undriven_variable_is_flagged(self):
        """A change that reaches nothing reads exactly like a safe change."""
        before = [var("mystery", "1 in")]
        after = [var("mystery", "2 in")]
        events, problems = detect_changes(before, after)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("drives nothing", problems[0])
        self.assertEqual(undriven_changes(events), events)

    def test_declared_variable_is_not_flagged(self):
        after = [*BASE_TABLE[:2], var("barArea", "0.4 in^2", "ANY")]
        events, problems = detect_changes(BASE_TABLE, after)
        self.assertEqual(len(events), 1)
        self.assertEqual(problems, [])


# --------------------------------------------------------------------------
# Branch sandbox, against a fake transport
# --------------------------------------------------------------------------

VALUE_PARAMETER = {
    "LENGTH": "lengthValue",
    "ANGLE": "angleValue",
    "NUMBER": "numberValue",
    "ANY": "anyValue",
}


def assign_variable_feature(feature_id: str, variable: Variable) -> dict:
    """A BTMFeature wrapper shaped like the real assignVariable payload.

    Matches `tests/fixtures/onshape/partstudios_d_w_e_features.json`: one
    value slot per variable type, and the authored text in `expression`.
    """
    value_id = VALUE_PARAMETER[variable.type]
    parameters = [
        {"typeName": "BTMParameterString",
         "message": {"parameterId": "name", "value": variable.name}},
        {"typeName": "BTMParameterEnum",
         "message": {"parameterId": "variableType", "value": variable.type}},
    ]
    # Every type slot exists on the real feature; only the matching one is read.
    for vtype, pid in VALUE_PARAMETER.items():
        parameters.append({
            "typeName": "BTMParameterQuantity",
            "message": {
                "parameterId": pid,
                "value": 0.0,
                "expression": variable.expression if pid == value_id else "",
            },
        })
    return {
        "type": 134,
        "typeName": "BTMFeature",
        "message": {
            "featureType": "assignVariable",
            "featureId": feature_id,
            "name": "###name = #value",
            "parameters": parameters,
        },
    }


def variable_from_feature(feature: dict) -> dict:
    """The read-only /variables row Onshape derives from an assignVariable
    feature -- note `value: null`, exactly as the live API reports."""
    body = feature["message"]
    params = {p["message"]["parameterId"]: p["message"] for p in body["parameters"]}
    vtype = params["variableType"]["value"]
    return {
        "name": params["name"]["value"],
        "type": vtype,
        "value": None,
        "expression": params[VALUE_PARAMETER[vtype]]["expression"],
        "description": "",
    }


class FakeOnshape(OnshapeClient):
    """Records calls and answers them from memory. Subclasses the real client
    so the code under test exercises the real request-building path.

    Features are the source of truth and `/variables` is DERIVED from them,
    because that is how the real document behaves: these variables are
    `assignVariable` features, and POSTing to /variables 404s. Modelling it the
    other way round would let a write succeed here that fails live.
    """

    def __init__(self, *, deny: tuple[str, str] | None = None):
        super().__init__(
            config=OnshapeConfig(
                access_key="k", secret_key="s",
                base_url="https://example.invalid",
                document_id="doc1", workspace_id="main1",
            )
        )
        self.calls: list[tuple[str, str, object]] = []
        self.workspaces: set[str] = {"main1"}
        self.versions: list[str] = []
        self.features: dict[str, list[dict]] = {
            "main1": [
                assign_variable_feature(f"F{i}", v)
                for i, v in enumerate(BASE_TABLE)
            ]
        }
        # (method, path fragment) that should answer 403, for failure tests.
        self.deny = deny

    def _request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        # Log exactly as the real transport does, so call-accounting behaviour
        # is under test rather than faked away.
        self.log.append(RequestRecord(method, path, 200, 0))
        if self.deny and self.deny[0] == method and self.deny[1] in path:
            raise OnshapeError(403, "denied", path)

        if method == "POST" and path.endswith("/versions"):
            vid = f"v{len(self.versions) + 1}"
            self.versions.append(vid)
            return {"id": vid, "name": body["name"]}

        if method == "POST" and path.endswith("/workspaces"):
            wid = f"w{len(self.workspaces)}"
            self.workspaces.add(wid)
            # A branch starts as a copy of Main.
            self.features[wid] = copy.deepcopy(self.features["main1"])
            return {"id": wid, "name": body["name"]}

        if method == "DELETE" and "/workspaces/" in path:
            self.workspaces.discard(path.rsplit("/", 1)[-1])
            return None

        wid = path.split("/w/")[1].split("/")[0] if "/w/" in path else None

        if "/variables/" in path:
            if method != "GET":
                # Exactly what the live API does: there is no variable table.
                raise OnshapeError(404, "Not found.", path)
            return [{
                "variableStudioReference": None,
                "variables": [
                    variable_from_feature(f) for f in self.features[wid]
                ],
            }]

        if "/features/featureid/" in path and method == "POST":
            feature_id = path.rsplit("/", 1)[-1]
            replacement = body["feature"]
            self.features[wid] = [
                replacement if f["message"]["featureId"] == feature_id else f
                for f in self.features[wid]
            ]
            return {"feature": replacement}

        if path.endswith("/features") and method == "GET":
            return {"features": self.features[wid]}

        raise AssertionError(f"unexpected call {method} {path}")


class TestBranchLifecycle(unittest.TestCase):
    def setUp(self):
        self.client = FakeOnshape()

    def test_open_creates_a_version_and_a_workspace(self):
        session = BranchSession.open(self.client, label="chg-001")
        self.assertEqual(len(self.client.versions), 1)
        self.assertIn(session.info.workspace_id, self.client.workspaces)
        session.reset()

    def test_branch_client_points_at_the_branch_not_main(self):
        """The whole safety property: a write lands in the sandbox by default."""
        with BranchSession.open(self.client, label="chg-001") as session:
            self.assertEqual(
                session.client.config.workspace_id, session.info.workspace_id
            )
            self.assertNotEqual(session.client.config.workspace_id, "main1")

    def test_context_manager_deletes_the_branch(self):
        with BranchSession.open(self.client, label="chg-001") as session:
            workspace_id = session.info.workspace_id
            self.assertIn(workspace_id, self.client.workspaces)
        self.assertNotIn(workspace_id, self.client.workspaces)

    def test_branch_is_deleted_even_when_the_body_raises(self):
        """An abandoned branch is a workspace nobody cleans up."""
        with self.assertRaises(RuntimeError):
            with BranchSession.open(self.client, label="chg-001") as session:
                workspace_id = session.info.workspace_id
                raise RuntimeError("analysis blew up")
        self.assertNotIn(workspace_id, self.client.workspaces)

    def test_keep_leaves_the_branch_in_place(self):
        with BranchSession.open(self.client, label="x", keep=True) as session:
            workspace_id = session.info.workspace_id
        self.assertIn(workspace_id, self.client.workspaces)

    def test_reuse_existing_creates_no_version(self):
        """The cheap path for a batch of eval scenarios."""
        first = BranchSession.open(self.client, label="a")
        workspace_id = first.info.workspace_id
        first.keep = True
        first.reset()

        before = len(self.client.versions)
        with BranchSession.open(self.client, reuse_existing=workspace_id) as second:
            self.assertTrue(second.info.reused)
            self.assertFalse(second.info.left_a_permanent_version)
        self.assertEqual(len(self.client.versions), before)
        self.assertIn(workspace_id, self.client.workspaces)

    def test_version_creation_is_announced_as_permanent(self):
        session = BranchSession.open(self.client, label="x")
        self.assertTrue(any("PERMANENT" in n for n in session.notes))
        session.reset()

    def test_reset_is_idempotent(self):
        session = BranchSession.open(self.client, label="x")
        session.reset()
        session.reset()      # must not raise or double-delete

    def test_branch_calls_are_folded_into_the_parent_log(self):
        """Reported cost must not understate what the run actually spent."""
        with BranchSession.open(self.client, label="x") as session:
            session.variables(PARTSTUDIO)
            session.variables(PARTSTUDIO)
        paths = [c[1] for c in self.client.calls if "/variables/" in c[1]]
        self.assertEqual(len(paths), 2)
        self.assertTrue(
            any("/variables/" in r.path for r in self.client.log),
            "branch reads never reached the parent log",
        )


class TestBranchEvaluate(unittest.TestCase):
    def setUp(self):
        self.client = FakeOnshape()

    def test_set_variable_changes_only_the_named_row(self):
        with BranchSession.open(self.client, label="x") as session:
            table = session.set_variable(PARTSTUDIO, "barArea", "0.4 in^2")
        values = {v.name: v.expression for v in table}
        self.assertEqual(values["barArea"], "0.4 in^2")
        self.assertEqual(values["bayWidth"], "360 in")
        self.assertEqual(values["bayHeight"], "360 in")

    def test_set_variable_preserves_the_row_type(self):
        """barArea is ANY because Onshape has no Area type; rewriting it as
        LENGTH would change what the expression means."""
        with BranchSession.open(self.client, label="x") as session:
            table = session.set_variable(PARTSTUDIO, "barArea", "0.4 in^2")
        self.assertEqual({v.name: v.type for v in table}["barArea"], "ANY")

    def test_unknown_variable_is_refused_rather_than_created(self):
        """A typo would otherwise read as a new variable, not a failed edit."""
        with BranchSession.open(self.client, label="x") as session:
            with self.assertRaises(KeyError) as ctx:
                session.set_variable(PARTSTUDIO, "barArae", "0.4 in^2")
            self.assertIn("no assignVariable feature", str(ctx.exception))

    def test_main_is_untouched_by_a_branch_write(self):
        """The property the whole sandbox exists for."""
        with BranchSession.open(self.client, label="x") as session:
            session.set_variable(PARTSTUDIO, "barArea", "0.4 in^2")
        main = {
            r["name"]: r["expression"]
            for r in map(variable_from_feature, self.client.features["main1"])
        }
        self.assertEqual(main["barArea"], "1 in^2")

    def test_evaluate_in_branch_applies_then_cleans_up(self):
        with evaluate_in_branch(
            self.client, PARTSTUDIO, {"barArea": "0.4 in^2"}, label="x"
        ) as session:
            workspace_id = session.info.workspace_id
            applied = {v.name: v.expression for v in session.variables(PARTSTUDIO)}
        self.assertEqual(applied["barArea"], "0.4 in^2")
        self.assertNotIn(workspace_id, self.client.workspaces)

    def test_before_and_after_tables_diff_into_a_change_event(self):
        """The join between the sandbox and change detection."""
        with BranchSession.open(self.client, label="x") as session:
            before = session.variables(PARTSTUDIO)
            after = session.set_variable(PARTSTUDIO, "barArea", "0.4 in^2")

        events, problems = detect_changes(before, after)
        self.assertEqual(problems, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].target_id, "barArea")
        self.assertEqual(events[0].value_before, "1 in^2")
        self.assertEqual(events[0].value_after, "0.4 in^2")


class TestBranchSessionHasNoMerge(unittest.TestCase):
    def test_session_exposes_no_merge(self):
        """plan.md: merge permission does not exist for the model at all. The
        object the analysis holds must not offer it."""
        self.assertFalse(
            [n for n in dir(BranchSession) if "merge" in n.lower()]
        )


class TestVariableFeaturesAgainstRealFixture(unittest.TestCase):
    """The recorded part studio features, holding the three real
    `assignVariable` features the document actually has."""

    def setUp(self):
        import json

        self.payload = json.loads(
            (ROOT / "tests" / "fixtures" / "onshape"
             / "partstudios_d_w_e_features.json").read_text(encoding="utf-8")
        )
        self.features = parse_variable_features(self.payload)

    def test_finds_all_three_variables(self):
        self.assertEqual(
            sorted(f.name for f in self.features),
            ["barArea", "bayHeight", "bayWidth"],
        )

    def test_ignores_the_geometry_feature(self):
        """The part studio also holds the tenBarTruss custom feature."""
        self.assertEqual(len(self.payload["features"]), 4)
        self.assertEqual(len(self.features), 3)

    def test_reads_the_authored_expressions(self):
        values = {f.name: f.expression for f in self.features}
        self.assertEqual(values["bayWidth"], "360 in")
        self.assertEqual(values["bayHeight"], "360 in")
        self.assertEqual(values["barArea"], "1 in^2")

    def test_value_parameter_follows_the_variable_type(self):
        """barArea is ANY because Onshape has no Area type, so its value lives
        in anyValue -- writing it into lengthValue would be ignored."""
        by_name = {f.name: f for f in self.features}
        self.assertEqual(by_name["barArea"].variable_type, "ANY")
        self.assertEqual(by_name["barArea"].value_parameter_id, "anyValue")
        self.assertEqual(by_name["bayWidth"].value_parameter_id, "lengthValue")

    def test_rewrite_changes_only_the_matching_slot(self):
        target = {f.name: f for f in self.features}["barArea"]
        updated = feature_with_expression(target, "0.4 in^2")
        params = {
            p["message"]["parameterId"]: p["message"]
            for p in updated["message"]["parameters"]
        }
        self.assertEqual(params["anyValue"]["expression"], "0.4 in^2")
        # The unused slots keep whatever Onshape defaulted them to (a real
        # feature carries "0.0*m", not an empty string) -- untouched, not
        # blanked.
        original = {
            q["message"]["parameterId"]: q["message"]
            for q in target.raw["message"]["parameters"]
        }
        self.assertEqual(
            params["lengthValue"]["expression"],
            original["lengthValue"]["expression"],
        )

    def test_rewrite_does_not_mutate_the_original(self):
        """The caller still needs the before-value to build the ChangeEvent."""
        target = {f.name: f for f in self.features}["barArea"]
        feature_with_expression(target, "0.4 in^2")
        self.assertEqual(target.expression, "1 in^2")

    def test_rewrite_preserves_every_other_parameter(self):
        """Onshape replaces the feature with what it is given, so a payload
        that drops parameters silently resets them."""
        target = {f.name: f for f in self.features}["barArea"]
        updated = feature_with_expression(target, "0.4 in^2")
        self.assertEqual(
            len(updated["message"]["parameters"]),
            len(target.raw["message"]["parameters"]),
        )


class TestVariablesEndpointIsReadOnly(unittest.TestCase):
    """Verified against the live API: GET works, POST 404s, because these
    variables are part studio features and there is no Variable Studio."""

    def test_client_offers_no_variable_table_write(self):
        self.assertFalse(hasattr(OnshapeClient, "set_variables"))

    def test_posting_to_variables_is_refused(self):
        client = FakeOnshape()
        with self.assertRaises(OnshapeError) as ctx:
            client._post("/api/variables/d/doc1/w/main1/e/ps/variables", [])
        self.assertEqual(ctx.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
