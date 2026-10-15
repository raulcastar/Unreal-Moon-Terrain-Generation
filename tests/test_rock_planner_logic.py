"""Rock Field & Rover Paths logic (gui.rock_planner_logic) and drawing (gui.rock_planner_plots)."""

import numpy as np
import pytest
from matplotlib.figure import Figure

import rock_field
from gui import rock_planner_logic as logic
from gui import rock_planner_plots as plots


def test_schema_keys_are_rock_field_parameters():
    params = rock_field.default_params()
    keys = [spec[0] for _group, specs in logic.ROCK_PARAM_GROUPS for spec in specs]
    assert keys and all(key in params for key in keys)


@pytest.mark.parametrize("text, value", [("2.5", 2.5), ("0", 1e-6), ("-3", 1e-6), ("abc", 1.0)])
def test_parse_resolution(text, value):
    assert logic.parse_resolution(text) == value


@pytest.mark.parametrize("fields, parsed", [
    (("9", "10", "0.15"), (9, 10, 0.15)), (("0", "10", "0.15"), None), (("9", "256", "0.1"), None),
    (("9", "10", "-1"), None), (("x", "10", "0.1"), None), (("9", "10", "0"), (9, 10, 0.0))])
def test_parse_stencils(fields, parsed):
    assert logic.parse_stencils(*fields) == parsed


def test_rocks_csv_and_row_count(tmp_path):
    path = logic.rocks_csv_path(str(tmp_path), "map_a")
    assert path.endswith("map_a_rocks.csv") and "map_a" in path.replace("map_a_rocks.csv", "")
    assert logic.count_rows(path) == 1                                  # missing file
    (tmp_path / "map_a").mkdir()
    with open(path, "w") as handle:
        handle.write("id,row,col,x_m,y_m,scale,height_m\n" + "1,0,0,0,0,0.2,0.1\n" * 4)
    assert logic.count_rows(path) == 4
    assert logic.read_rock_scales(path).tolist() == [0.2] * 4


def test_export_confirmation():
    text = logic.export_confirmation("dem.npy", [("map_rocks.csv", "rocks")], "    C:/out")
    assert text.startswith("Plan the rock field on dem.npy") and "map_rocks.csv" in text
    assert text.endswith("Continue?")


def test_model_info():
    assert "unknown" in logic.model_info_text(None, None, 0.8)
    ok = logic.model_info_text((0.1, 0.4, 0.8), (0.9, 1.2), 0.8)
    assert "Planned radii scale them 0.9x-1.2x" in ok and "height_m" not in ok
    # Scaling the models far from their own size is expected (small scans used as
    # boulders): the read-out states it without asking to change anything.
    scaled = logic.model_info_text((0.04, 0.13, 1.2), (1.6, 23.0), 0.7)
    assert "1.6x-23x" in scaled and "Match" not in scaled
    # height_m is a maximum: taller models are flattened, squatter ones stay lower.
    assert "flattens them to height_m" in scaled
    squat = logic.model_info_text((0.1, 0.4, 0.3), (0.9, 1.2), 0.7)
    assert "stay lower than height_m" in squat


def test_rock_estimate_text():
    small = logic.rock_estimate_text(1200, 50_000, True)
    assert small == "About 1,200 rocks on the drivable ground."
    capped = logic.rock_estimate_text(80_000, 50_000, False)
    assert "whole map" in capped and "limit" in capped and "Placing them in Unreal" in capped
    assert "actor" not in capped                                         # fields are instanced now


def test_stencil_split_text():
    text = logic.stencil_split_text([0.1, 0.2, 0.3], 9, 10, 0.15)
    assert text == "This field: 2 boulders (id 9), 1 small rocks (id 10)."
    assert "All rocks fall in one class" in logic.stencil_split_text([0.1, 0.12], 9, 10, 0.15)


def result(**overrides):
    base = dict(rock_count=5, drivable_frac=0.8, crater_frac=0.2, free_frac=0.75,
                road_segments=3, connected=True)
    base.update(overrides)
    return base


def test_result_log_lines():
    lines = logic.result_log_lines(result(), 50_000)
    assert lines[0][0].startswith("Placed 5 rocks") and lines[-1] == (
        "Voronoi road network is one connected route.", "ok")
    capped = logic.result_log_lines(result(rock_capped=True, rock_requested=60_000), 50_000)
    assert capped[0][1] == "warn" and "60,000" in capped[0][0]
    assert logic.result_log_lines(result(road_segments=0), 1)[-1][1] == "warn"
    assert logic.route_status(result(connected=False)) == "route split by terrain"
    assert logic.route_status(result(road_segments=0)) == "no craters found for roads"


def test_plan_drawing():
    rng = np.random.default_rng(0)
    plan = rng.normal(size=(60, 80)).cumsum(axis=1)
    crater = np.zeros((60, 80), bool)
    crater[10:20, 10:20] = True
    shown = result(plan_elevation=plan, plan_res=(1.0, 1.0), plan_shape=(60, 80),
                   crater_mask=crater, paths=[(np.arange(10.0), np.arange(10.0))],
                   markers=[(15, 15)], marker_label="Crater centre (site)",
                   rocks_px=np.array([[30.0, 40.0, 1.5], [5.0, 70.0, 0.5]]))
    figure = Figure()
    plots.draw_plan(figure, shown)
    figure.canvas.draw()
    assert "5 rocks" in figure.axes[0].get_title()
    plots.draw_message(figure, "Loading ...")
    figure.canvas.draw()
    assert figure.axes[0].texts[0].get_text() == "Loading ..."


def test_an_export_remembers_its_settings_and_flags_changes(tmp_path):
    """Placing compares the exported field's settings with the controls (Export & Unreal tab)."""
    import rock_field
    params = rock_field.default_params()
    exported = logic.export_settings(params, 1, 1.0, str(tmp_path / "map.npy"), "generator")
    rocks_csv = str(tmp_path / "map_rocks.csv")
    logic.save_settings(rocks_csv, exported)
    assert logic.load_settings(rocks_csv) == exported
    assert logic.settings_changes(logic.load_settings(rocks_csv), exported) == []

    changed = logic.export_settings({**params, "ROCK_COVERAGE": 0.02}, 2, 1.0,
                                    str(tmp_path / "map.npy"), "detected")
    assert logic.settings_changes(exported, changed) == ["coverage", "seed", "crater source"]
    text, level = logic.export_status(True, ["coverage", "seed"])
    assert level == "warn" and "coverage, seed" in text and "old field" in text
    assert logic.export_status(True, [])[1] == "ok"
    assert "No rock field" in logic.export_status(False, [])[0]
    assert "not recorded" in logic.export_status(True, None)[0]
    assert logic.load_settings(str(tmp_path / "other_rocks.csv")) is None
    # The boulder threshold only splits rocks between mask ids when placing: the
    # exported file stays current when it changes.
    reclassed = logic.export_settings({**params, "BOULDER_MIN_RADIUS_M": 1.0}, 1, 1.0,
                                      str(tmp_path / "map.npy"), "generator")
    assert logic.settings_changes(exported, reclassed) == []
