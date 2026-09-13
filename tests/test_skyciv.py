"""SkyCiv client tests -- fully offline via an injected transport. [Track B, 3B]

The request payload shape is unverified against the live API (see the module
docstring), so what these tests pin is our OWN behaviour: SI -> SkyCiv metric
conversion, id mapping, restraint codes, the function order and auth of the
two calls, what is fatal and what is only a warning, and shape-tolerant
parsing of the documented response layouts.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from contract_selfcheck import AREA, E_STEEL, IN, P, YIELD, build_graph  # noqa: E402

from earl.artifacts import skyciv_client as sc  # noqa: E402
from earl.artifacts.skyciv_client import (  # noqa: E402
    DEFAULT_DESIGN_CODE,
    FN_DESIGN_CHECK,
    FN_DESIGN_INPUT,
    FN_MODEL_SET,
    FN_MODEL_SOLVE,
    FN_REPORT,
    FN_SESSION_START,
    SKYCIV_UNITS,
    S3DModel,
    SkyCivClient,
    SkyCivError,
    SkyCivRun,
    build_s3d_model,
    parse_design_results,
    parse_member_results,
    parse_report_link,
)
from earl.config import SkyCivConfig  # noqa: E402
from earl.contracts import (  # noqa: E402
    ChangeEvent,
    ChangeKind,
    DependencyGraph,
    LoadCase,
    Material,
    Member,
    Node,
    OnshapeRef,
    PointLoad,
    Section,
    SupportType,
)

FIXTURES = ROOT / "tests" / "fixtures" / "skyciv" / "synthetic"
LBF = 4.4482216

# The PyNite equal-area forces the fixture was built from (lb, tension +).
EQUAL_AREA_FORCES_LB = {
    "m1": 195364.99, "m2": 40124.63, "m3": -204635.01, "m4": -59875.37,
    "m5": 35489.62, "m6": 40124.63, "m7": 147976.25, "m8": -134866.46,
    "m9": 84676.56, "m10": -56744.80,
}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_config() -> SkyCivConfig:
    # Never read the developer's .env: explicit fake credentials.
    return SkyCivConfig(username="tester@example.com", key="not-a-real-key")


class FakeTransport:
    """Returns canned responses in order and remembers every payload."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if not self.responses:
            raise AssertionError("transport called more times than expected")
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def build_3d_tripod() -> DependencyGraph:
    """A non-planar 3-bar tripod: no coordinate is constant across nodes."""
    nodes = [
        Node("a", 1.0, 0.0, 0.0, support=SupportType.PIN),
        Node("b", -0.5, 0.0, 0.8, support=SupportType.PIN),
        Node("c", -0.5, 0.0, -0.8, support=SupportType.PIN),
        Node("apex", 0.0, 2.0, 0.0),
    ]
    members = [
        Member("t1", "a", "apex", "sec", "mat"),
        Member("t2", "b", "apex", "sec", "mat"),
        Member("t3", "c", "apex", "sec", "mat"),
    ]
    return DependencyGraph(
        id="graph-tripod",
        change=ChangeEvent("chg-t", ChangeKind.FEATURE_EDIT, "n/a", "t1"),
        source=OnshapeRef("d", "w", "e"),
        nodes=nodes,
        members=members,
        materials=[Material("mat", "steel", 2.0e11, 2.5e8, density=7850.0)],
        sections=[Section("sec", "bar", 1e-4, iy=1e-9, iz=2e-9, j=3e-9)],
        load_cases=[
            LoadCase("lc_sw", "gravity", [PointLoad("p", "apex", fy=-1000.0)],
                     include_self_weight=True)
        ],
    )


# ---------------------------------------------------------------------------
# build_s3d_model
# ---------------------------------------------------------------------------

class TestBuildS3DModel(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()
        self.s3d = build_s3d_model(self.graph, "lc_1")
        self.model = self.s3d.model

    def test_returns_companion_dataclass_with_index_maps(self):
        self.assertIsInstance(self.s3d, S3DModel)
        self.assertEqual(self.s3d.node_index, {f"n{i}": i for i in range(1, 7)})
        self.assertEqual(self.s3d.member_index, {f"m{i}": i for i in range(1, 11)})

    def test_settings_units_and_axis(self):
        self.assertEqual(self.model["settings"]["units"], SKYCIV_UNITS)
        self.assertEqual(self.model["settings"]["vertical_axis"], "Y")
        self.assertTrue(self.model["settings"]["auto_stabilize_model"])

    def test_nodes_are_metres_with_one_based_string_keys(self):
        self.assertEqual(set(self.model["nodes"]), {str(i) for i in range(1, 7)})
        n1 = self.model["nodes"]["1"]
        self.assertAlmostEqual(n1["x"], 720 * IN)
        self.assertAlmostEqual(n1["y"], 360 * IN)
        self.assertEqual(n1["z"], 0.0)

    def test_section_area_in_mm2_and_inertia_in_mm4(self):
        (sec,) = self.model["sections"].values()
        self.assertAlmostEqual(sec["area"], AREA * 1e6)          # 1 in^2 = 645.16 mm^2
        self.assertAlmostEqual(sec["area"], 645.16, places=6)
        self.assertEqual(sec["material_id"], 1)
        for key in ("Iy", "Iz", "J"):
            self.assertEqual(sec[key], 0.0)

    def test_material_in_mpa(self):
        (mat,) = self.model["materials"].values()
        self.assertAlmostEqual(mat["elasticity_modulus"], E_STEEL * 1e-6)
        self.assertAlmostEqual(mat["yield_strength"], YIELD * 1e-6)
        self.assertAlmostEqual(mat["ultimate_strength"], 1.3 * YIELD * 1e-6)
        self.assertEqual(mat["density"], 7850.0)
        self.assertEqual(mat["poissons_ratio"], 0.3)
        self.assertEqual(mat["class"], "steel")

    def test_members_map_nodes_and_release_bending(self):
        m7 = self.model["members"]["7"]       # m7: n5 -> n4
        self.assertEqual(m7["node_A"], 5)
        self.assertEqual(m7["node_B"], 4)
        self.assertEqual(m7["type"], "normal_continuous")
        self.assertEqual(m7["fixity_A"], "FFFFRR")
        self.assertEqual(m7["fixity_B"], "FFFFRR")
        self.assertEqual(m7["section_id"], 1)
        self.assertEqual(m7["rotation_angle"], 0)

    def test_point_loads_in_kn_with_type_n(self):
        loads = self.model["point_loads"]
        self.assertEqual(len(loads), 2)
        p1 = loads["1"]
        self.assertEqual(p1["type"], "N")
        self.assertEqual(p1["node"], 2)                         # n2
        self.assertAlmostEqual(p1["y_mag"], -P / 1000.0)        # -444.822 kN
        self.assertEqual(p1["x_mag"], 0.0)
        self.assertEqual(p1["load_group"], "LG1")

    def test_planar_support_codes(self):
        """z is constant, so every node has Tz fixed and all rotations fixed."""
        supports = self.model["supports"]
        self.assertEqual(len(supports), 6)               # every node, even FREE
        self.assertEqual(supports["5"]["restraint_code"], "FFFFFF")   # PIN
        self.assertEqual(supports["1"]["restraint_code"], "RRFFFF")   # FREE, planar
        self.assertEqual(supports["1"]["node"], 1)

    def test_roller_codes(self):
        self.graph.node("n6").support = SupportType.ROLLER_X
        self.graph.node("n5").support = SupportType.ROLLER_Y
        model = build_s3d_model(self.graph, "lc_1").model
        self.assertEqual(model["supports"]["6"]["restraint_code"], "RFFFFF")
        self.assertEqual(model["supports"]["5"]["restraint_code"], "FRFFFF")
        self.graph.node("n5").support = SupportType.FIXED
        model = build_s3d_model(self.graph, "lc_1").model
        self.assertEqual(model["supports"]["5"]["restraint_code"], "FFFFFF")

    def test_no_self_weight_unless_requested(self):
        self.assertEqual(self.model["self_weight"], {})
        self.assertEqual(self.model["load_combinations"]["1"], {"name": "lc_1", "LG1": 1})

    def test_self_weight_when_requested(self):
        self.graph.load_cases[0].include_self_weight = True
        model = build_s3d_model(self.graph, "lc_1").model
        self.assertEqual(model["self_weight"]["1"], {"x": 0, "y": -1, "z": 0, "LG": "SW1"})
        self.assertEqual(model["load_combinations"]["1"]["SW1"], 1)
        self.assertEqual(model["load_combinations"]["1"]["LG1"], 1)

    def test_unknown_load_case_rejected(self):
        with self.assertRaises(ValueError):
            build_s3d_model(self.graph, "nope")

    def test_non_si_graph_rejected(self):
        from earl.contracts import Units, UnitSystem

        self.graph.units = Units(system=UnitSystem.IMPERIAL)
        with self.assertRaises(ValueError):
            build_s3d_model(self.graph, "lc_1")

    def test_3d_truss_free_node_keeps_translations_free(self):
        s3d = build_s3d_model(build_3d_tripod(), "lc_sw")
        self.assertEqual(s3d.model["supports"]["4"]["restraint_code"], "RRRFFF")
        self.assertEqual(s3d.model["supports"]["1"]["restraint_code"], "FFFFFF")
        sec = s3d.model["sections"]["1"]
        self.assertAlmostEqual(sec["Iy"], 1e-9 * 1e12)
        self.assertAlmostEqual(sec["Iz"], 2e-9 * 1e12)
        self.assertAlmostEqual(sec["J"], 3e-9 * 1e12)
        self.assertIn("1", s3d.model["self_weight"])


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class TestParseMemberResults(unittest.TestCase):
    def setUp(self):
        self.member_index = {f"m{i}": i for i in range(1, 11)}
        fixture = load_fixture("solve_response.json")
        solve = next(f for f in fixture["functions"] if f["function"] == FN_MODEL_SOLVE)
        self.data = solve["data"]

    def test_fixture_is_marked_synthetic(self):
        for name in ("solve_response.json", "solve_response_list_combos.json",
                     "design_check_response.json", "solve_failed_response.json"):
            self.assertIn("_comment", load_fixture(name))

    def test_every_member_parsed_with_kn_to_n(self):
        results = parse_member_results(self.data, self.member_index)
        self.assertEqual(set(results), set(self.member_index))
        for mid, lb in EQUAL_AREA_FORCES_LB.items():
            expected_n = lb * LBF
            self.assertAlmostEqual(results[mid].axial_force, expected_n, delta=abs(expected_n) * 1e-5)
            self.assertEqual(results[mid].member_id, mid)

    def test_shapes_scalar_flatlist_pairs_dict_all_handled(self):
        # m2 scalar, m6 flat list, m1 [position, value] pairs, m8 dict
        results = parse_member_results(self.data, self.member_index)
        self.assertAlmostEqual(results["m2"].axial_force, 178.4832e3, delta=1.0)
        self.assertAlmostEqual(results["m6"].axial_force, 178.4832e3, delta=1.0)
        self.assertAlmostEqual(results["m1"].axial_force, 869.0268e3, delta=1.0)
        self.assertAlmostEqual(results["m8"].axial_force, -599.9159e3, delta=1.0)

    def test_stress_mpa_to_pa(self):
        results = parse_member_results(self.data, self.member_index)
        # stress = F / A, A = 645.16 mm^2
        expected = results["m3"].axial_force / (AREA)
        self.assertAlmostEqual(results["m3"].stress, expected, delta=abs(expected) * 1e-4)
        self.assertLess(results["m3"].stress, 0.0)

    def test_sign_hint_flips(self):
        flipped = parse_member_results(self.data, self.member_index, sign_hint=-1.0)
        self.assertLess(flipped["m1"].axial_force, 0.0)
        self.assertGreater(flipped["m3"].axial_force, 0.0)

    def test_single_combination_object(self):
        combo = self.data["1"]
        results = parse_member_results(combo, self.member_index)
        self.assertEqual(len(results), 10)

    def test_list_of_combination_objects(self):
        fixture = load_fixture("solve_response_list_combos.json")
        solve = next(f for f in fixture["functions"] if f["function"] == FN_MODEL_SOLVE)
        results = parse_member_results(solve["data"], self.member_index)
        self.assertEqual(len(results), 10)
        self.assertIsNone(results["m1"].stress)
        self.assertAlmostEqual(results["m5"].axial_force, 157.8657e3, delta=1.0)

    def test_max_abs_reduction_over_positions(self):
        data = {"member_forces": {"1": {"axial": [[0, -2.0], [50, 5.0], [100, -7.5]]}}}
        results = parse_member_results(data, {"m1": 1})
        self.assertAlmostEqual(results["m1"].axial_force, -7500.0)

    def test_unrecognised_shape_gives_empty(self):
        self.assertEqual(parse_member_results("garbage", self.member_index), {})
        self.assertEqual(parse_member_results(None, self.member_index), {})
        self.assertEqual(parse_member_results({"foo": "bar"}, self.member_index), {})

    def test_unknown_member_ids_ignored(self):
        data = {"member_forces": {"99": {"axial": 1.0}, "1": {"axial": 2.0}}}
        results = parse_member_results(data, {"m1": 1})
        self.assertEqual(list(results), ["m1"])


class TestParseReportLink(unittest.TestCase):
    def test_top_level_download_key(self):
        self.assertEqual(parse_report_link({"download_link": "https://x/y.pdf"}), "https://x/y.pdf")

    def test_nested_and_url_key(self):
        data = {"report": {"file": "y.pdf", "url": "https://x/z.pdf"}}
        self.assertEqual(parse_report_link(data), "https://x/z.pdf")

    def test_bare_string(self):
        self.assertEqual(parse_report_link("https://x/a.pdf"), "https://x/a.pdf")
        self.assertIsNone(parse_report_link("not a url"))

    def test_missing(self):
        self.assertIsNone(parse_report_link({"status": "ok"}))
        self.assertIsNone(parse_report_link(None))


class TestParseDesignResults(unittest.TestCase):
    def setUp(self):
        self.member_index = {f"m{i}": i for i in range(1, 11)}

    def test_fixture_dict_under_results(self):
        fixture = load_fixture("design_check_response.json")
        data = fixture["functions"][0]["data"]
        results = parse_design_results(data, self.member_index)
        self.assertEqual(len(results), 10)
        self.assertAlmostEqual(results["m3"].ratio, 0.80)
        self.assertTrue(results["m3"].passed)

    def test_flat_dict_and_list_forms(self):
        flat = {"1": {"utilization": 1.2, "status": "FAIL"}, "2": {"ratio": 0.4}}
        results = parse_design_results(flat, self.member_index)
        self.assertFalse(results["m1"].passed)
        self.assertTrue(results["m2"].passed)      # inferred from ratio <= 1
        listed = [{"member": 3, "ratio": 1.5}, {"member_id": "4", "ratio": 0.2, "pass": True}]
        results = parse_design_results(listed, self.member_index)
        self.assertFalse(results["m3"].passed)     # ratio > 1, no flag
        self.assertTrue(results["m4"].passed)

    def test_unrecognised(self):
        self.assertEqual(parse_design_results(None, self.member_index), {})
        self.assertEqual(parse_design_results({"1": "x"}, self.member_index), {})


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class TestSkyCivClient(unittest.TestCase):
    def setUp(self):
        self.graph = build_graph()

    def make_client(self, responses, **kwargs) -> tuple[SkyCivClient, FakeTransport]:
        transport = FakeTransport(responses)
        client = SkyCivClient(config=make_config(), transport=transport, **kwargs)
        return client, transport

    def test_happy_path_two_calls(self):
        client, transport = self.make_client(
            [load_fixture("solve_response.json"), load_fixture("design_check_response.json")]
        )
        run = client.analyze(self.graph, "lc_1")
        self.assertIsInstance(run, SkyCivRun)
        self.assertEqual(run.api_calls, 2)
        self.assertEqual(client.request_count, 2)
        self.assertEqual(run.session_id, "sess-synthetic-0001")
        self.assertEqual(run.report_url, "https://platform.skyciv.com/reports/synthetic-0001.pdf")
        self.assertEqual(run.design_code, DEFAULT_DESIGN_CODE)
        self.assertEqual(len(run.member_results), 10)
        self.assertEqual(len(run.design_results), 10)
        self.assertEqual(run.warnings, [])
        self.assertIn("call_1", run.raw)
        self.assertIn("call_2", run.raw)
        self.assertAlmostEqual(run.member_results["m1"].axial_force, 869.0268e3, delta=1.0)

    def test_function_order_and_auth(self):
        client, transport = self.make_client(
            [load_fixture("solve_response.json"), load_fixture("design_check_response.json")]
        )
        client.analyze(self.graph, "lc_1")
        first, second = transport.payloads

        names = [f["function"] for f in first["functions"]]
        self.assertEqual(
            names, [FN_SESSION_START, FN_MODEL_SET, FN_MODEL_SOLVE, FN_REPORT, FN_DESIGN_INPUT]
        )
        self.assertEqual(first["auth"], {"username": "tester@example.com", "key": "not-a-real-key"})
        self.assertEqual(first["options"], {"validate_input": True, "response_data_only": False})
        self.assertTrue(first["functions"][0]["arguments"]["keep_open"])      # design follows
        self.assertIn("s3d_model", first["functions"][1]["arguments"])
        self.assertEqual(first["functions"][1]["arguments"]["s3d_model"]["settings"]["units"], SKYCIV_UNITS)
        self.assertEqual(
            first["functions"][2]["arguments"], {"analysis_type": "linear", "repair_model": True}
        )
        self.assertEqual(first["functions"][3]["arguments"], {"file_type": "pdf"})
        self.assertEqual(first["functions"][4]["arguments"], {"design_code": DEFAULT_DESIGN_CODE})

        self.assertEqual([f["function"] for f in second["functions"]], [FN_DESIGN_CHECK])
        self.assertEqual(second["auth"]["session_id"], "sess-synthetic-0001")
        self.assertEqual(second["auth"]["username"], "tester@example.com")
        args = second["functions"][0]["arguments"]
        self.assertEqual(args["design_code"], DEFAULT_DESIGN_CODE)
        self.assertEqual(args["design_input"]["design_code"], DEFAULT_DESIGN_CODE)  # echoed from call 1
        self.assertNotIn("$previous", json.dumps(second))

    def test_no_design_code_means_single_call_and_session_closed(self):
        client, transport = self.make_client([load_fixture("solve_response.json")])
        run = client.analyze(self.graph, "lc_1", design_code=None)
        self.assertEqual(run.api_calls, 1)
        self.assertIsNone(run.design_code)
        self.assertEqual(run.design_results, {})
        (payload,) = transport.payloads
        self.assertFalse(payload["functions"][0]["arguments"]["keep_open"])
        self.assertNotIn(FN_DESIGN_INPUT, [f["function"] for f in payload["functions"]])

    def test_no_report_when_not_wanted(self):
        client, transport = self.make_client([load_fixture("solve_response.json")])
        run = client.analyze(self.graph, "lc_1", design_code=None, want_report=False)
        self.assertIsNone(run.report_url)
        self.assertNotIn(FN_REPORT, [f["function"] for f in transport.payloads[0]["functions"]])

    def test_mandatory_function_failure_names_the_function(self):
        client, _ = self.make_client([load_fixture("solve_failed_response.json")])
        with self.assertRaises(SkyCivError) as ctx:
            client.analyze(self.graph, "lc_1")
        self.assertIn(FN_MODEL_SOLVE, str(ctx.exception))
        self.assertIn("unstable", str(ctx.exception))
        self.assertEqual(ctx.exception.function, FN_MODEL_SOLVE)
        self.assertEqual(ctx.exception.status, 1)

    def test_missing_mandatory_entry_is_fatal(self):
        response = load_fixture("solve_response.json")
        response["functions"] = [f for f in response["functions"] if f["function"] != FN_MODEL_SET]
        client, _ = self.make_client([response])
        with self.assertRaises(SkyCivError) as ctx:
            client.analyze(self.graph, "lc_1")
        self.assertIn(FN_MODEL_SET, str(ctx.exception))

    def test_optional_report_failure_is_a_warning(self):
        response = load_fixture("solve_response.json")
        for f in response["functions"]:
            if f["function"] == FN_REPORT:
                f["status"] = 1
                f["msg"] = "Report generation timed out"
        client, _ = self.make_client([response, load_fixture("design_check_response.json")])
        run = client.analyze(self.graph, "lc_1")
        self.assertIsNone(run.report_url)
        self.assertEqual(len(run.member_results), 10)
        self.assertTrue(any(FN_REPORT in w and "timed out" in w for w in run.warnings))

    def test_optional_design_input_failure_skips_call_two(self):
        response = load_fixture("solve_response.json")
        for f in response["functions"]:
            if f["function"] == FN_DESIGN_INPUT:
                f["status"] = 2
                f["msg"] = "Unknown design code"
        client, transport = self.make_client([response])
        run = client.analyze(self.graph, "lc_1")
        self.assertEqual(run.api_calls, 1)
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(run.design_results, {})
        self.assertTrue(any(FN_DESIGN_INPUT in w for w in run.warnings))

    def test_missing_session_id_skips_call_two_with_warning(self):
        response = load_fixture("solve_response.json")
        del response["last_session_id"]
        client, transport = self.make_client([response])
        run = client.analyze(self.graph, "lc_1")
        self.assertIsNone(run.session_id)
        self.assertEqual(run.api_calls, 1)
        self.assertTrue(any("session id" in w for w in run.warnings))

    def test_design_check_failure_is_a_warning(self):
        second = load_fixture("design_check_response.json")
        second["functions"][0]["status"] = 1
        second["functions"][0]["msg"] = "Design module unavailable"
        client, _ = self.make_client([load_fixture("solve_response.json"), second])
        run = client.analyze(self.graph, "lc_1")
        self.assertEqual(run.api_calls, 2)
        self.assertEqual(run.design_results, {})
        self.assertTrue(any(FN_DESIGN_CHECK in w for w in run.warnings))

    def test_transport_network_error_becomes_skyciv_error(self):
        client, _ = self.make_client([requests.ConnectionError("dns failure")])
        with self.assertRaises(SkyCivError) as ctx:
            client.analyze(self.graph, "lc_1")
        self.assertIn("dns failure", str(ctx.exception))

    def test_call_does_not_check_function_status(self):
        """Per R14 the fatal/optional split is analyze()'s job, not call()'s."""
        client, _ = self.make_client([load_fixture("solve_failed_response.json")])
        data = client.call([{"function": FN_SESSION_START, "arguments": {}}], label="x")
        self.assertEqual(data["functions"][2]["status"], 1)

    def test_record_dir_writes_both_call_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_dir = Path(tmp) / "recorded"
            client, _ = self.make_client(
                [load_fixture("solve_response.json"), load_fixture("design_check_response.json")],
                record_dir=record_dir,
            )
            client.analyze(self.graph, "lc_1")
            one = record_dir / "skyciv_analyze_1.json"
            two = record_dir / "skyciv_analyze_2.json"
            self.assertTrue(one.exists())
            self.assertTrue(two.exists())
            self.assertEqual(json.loads(one.read_text())["last_session_id"], "sess-synthetic-0001")
            self.assertEqual(json.loads(two.read_text())["functions"][0]["function"], FN_DESIGN_CHECK)

    def test_download_report_uses_injected_downloader(self):
        def downloader(url: str, dest: Path) -> Path:
            dest.write_bytes(b"%PDF-stub " + url.encode())
            return dest

        client = SkyCivClient(config=make_config(), transport=FakeTransport([]), downloader=downloader)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "nested" / "report.pdf"
            out = client.download_report("https://x/report.pdf", dest)
            self.assertEqual(out, dest)
            self.assertTrue(dest.read_bytes().startswith(b"%PDF-stub"))


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestDefaultTransport(unittest.TestCase):
    """The default transport posts with `requests`; swap requests.post for a
    fake so no socket is ever opened."""

    def setUp(self):
        self._real_post = sc.requests.post
        self.posted: list[dict] = []

    def tearDown(self):
        sc.requests.post = self._real_post

    def install(self, response: FakeResponse) -> None:
        def fake_post(url, json=None, timeout=None, **kwargs):
            self.posted.append({"url": url, "json": json, "timeout": timeout})
            return response

        sc.requests.post = fake_post

    def test_http_error_raises_skyciv_error(self):
        self.install(FakeResponse(500, "Internal Server Error"))
        client = SkyCivClient(config=make_config())
        with self.assertRaises(SkyCivError) as ctx:
            client.call([{"function": FN_SESSION_START, "arguments": {}}], label="x")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("500", str(ctx.exception))
        self.assertEqual(client.log[-1].status, -1)

    def test_non_json_body_raises_skyciv_error(self):
        self.install(FakeResponse(200, "<html>login</html>"))
        client = SkyCivClient(config=make_config())
        with self.assertRaises(SkyCivError):
            client.call([{"function": FN_SESSION_START, "arguments": {}}], label="x")

    def test_posts_to_port_qualified_base_url_with_timeout(self):
        self.install(FakeResponse(200, "{}", payload={"functions": []}))
        client = SkyCivClient(config=make_config(), timeout=42)
        client.call([{"function": FN_SESSION_START, "arguments": {}}], label="x")
        self.assertEqual(self.posted[0]["url"], "https://api.skyciv.com:8085/v3")
        self.assertEqual(self.posted[0]["timeout"], 42)
        self.assertEqual(self.posted[0]["json"]["auth"]["key"], "not-a-real-key")
        self.assertEqual(client.log[-1].status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
