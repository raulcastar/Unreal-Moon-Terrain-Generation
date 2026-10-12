"""Generate view logic (gui.generator_logic) and drawing (gui.generator_plots)."""

import numpy as np
import pytest
from matplotlib.figure import Figure

import terrain_core
from gui import generator_logic as logic
from gui import generator_plots as plots


@pytest.fixture
def params():
    return terrain_core.coerce_params(terrain_core.default_params())


def test_schema_covers_known_parameters(params):
    keys = [spec[0] for _group, specs in logic.PARAM_GROUPS for spec in specs]
    assert len(keys) == len(set(keys))
    assert all(key in params for key in keys)
    assert all(spec[4] in ("int", "float", "log") and spec[2] < spec[3]
               for _group, specs in logic.PARAM_GROUPS for spec in specs)


@pytest.mark.parametrize("text, seed", [("42", 42), (" 7 ", 7), ("", None), ("abc", None), ("4.5", None)])
def test_parse_seed(text, seed):
    assert logic.parse_seed(text) == seed


def test_roll_seed_range():
    assert all(0 <= logic.roll_seed() < 2 ** 31 for _ in range(20))


@pytest.mark.parametrize("rows, cols, target, shape", [
    (1000, 2000, 256, (128, 256)), (100, 100, 256, (100, 100)), (4000, 40, 256, (256, 32))])
def test_preview_shape(rows, cols, target, shape):
    assert logic.preview_shape(rows, cols, target) == shape


def test_base_name_and_output_preview():
    assert logic.base_name_or_default("  ") == logic.DEFAULT_BASE_NAME
    assert logic.base_name_or_default(" map_a ") == "map_a"
    text = logic.output_preview_text("map_a", [("map_a_elevationMap.npy", "elevation")])
    assert text == "outputs/map_a/\n  map_a_elevationMap.npy"


def test_generation_confirmation(params):
    params = dict(params, ROWS=1000, COLS=2000)
    message = logic.generation_confirmation(params, 5, "    C:/out", [("a.npy", "elevation")])
    assert "2000 x 1000 px DEM (2.0 megapixels) with seed 5" in message
    assert "C:/out" in message and "a.npy" in message and message.endswith("Continue?")
    assert "large" not in message
    big = logic.generation_confirmation(dict(params, ROWS=6000, COLS=6000), 5, "", [])
    assert "This map is large" in big


def test_presets_listed_and_folder_created(tmp_path):
    folder = tmp_path / "config"
    assert logic.list_presets(str(folder)) == [] and folder.is_dir()
    for name in ("b.yaml", "a.yml", "notes.txt"):
        (folder / name).write_text("x")
    assert logic.list_presets(str(folder)) == ["a.yml", "b.yaml"]


def test_slope_of_a_plane():
    x = np.arange(20, dtype=float)
    plane = np.tile(x * np.tan(np.radians(30)), (10, 1))      # 30 deg along x, 1 m cells
    assert np.allclose(logic.slope_degrees(plane, 1.0)[2:-2, 2:-2], 30.0)


def test_display_geometry_of_a_preview(params):
    params = dict(params, ROWS=1000, COLS=2000, RESOLUTION_M_PER_PX=0.5)
    extent, res = logic.display_geometry(np.zeros((128, 256)), params)
    assert res == pytest.approx(0.5 * 2000 / 256)
    assert extent == (0, pytest.approx(1000.0), 0, pytest.approx(500.0))


def test_stats_lines(params):
    lines = logic.stats_lines(params, 123)
    assert lines[0].startswith("DEM grid") and "123" in lines[3] and len(lines) == 6
    rendered = {"elevation": np.linspace(0, 10, 64 * 64).reshape(64, 64), "crater_count": 9}
    full = logic.stats_lines(params, 123, rendered, full_resolution=True)
    assert "-- rendered (full resolution) --" in full and any("Craters placed  9" in l for l in full)
    assert "-- rendered (preview) --" in logic.stats_lines(params, 123, rendered)


def test_topology_text(params):
    assert "unavailable" in logic.topology_text(params, None, "no module")
    assert "50x50 component" in logic.topology_text(params, None)
    params = dict(params, ROWS=1000, COLS=1000, RESOLUTION_M_PER_PX=1.0)
    text = logic.topology_text(params, (8, 8, 63, 2, 1009, 1009))
    assert "Components      8 x 8" in text and "Edge padding    18 px" in text
    assert "XY scale        100 (cm/px)" in text


def test_title_suffix():
    shown = {"elevation": np.zeros((10, 20)), "seed": 3}
    assert logic.title_suffix(shown, False) == "20x10 preview - seed 3"
    assert logic.title_suffix(shown, True) == "20x10 FULL RESOLUTION - seed 3"


def test_plots_draw_on_a_figure():
    rng = np.random.default_rng(0)
    elevation = rng.normal(size=(80, 120)).cumsum(axis=0)
    extent = (0, 120.0, 0, 80.0)
    relief, surface, slope = Figure(), Figure(), Figure()
    plots.draw_relief(relief, elevation, extent, 1.0, "gray", 315, 25, 12, "t")
    artists = plots.draw_surface(surface, elevation, extent, 1.0, "gray", 315, 25, "t",
                                 mesh_detail=60, drag_detail=20)
    plots.draw_slope(slope, elevation, extent, 1.0, "t")
    assert len(relief.axes) == 2 and len(slope.axes) == 3       # with colour bars
    assert artists is not None and not artists[1].get_visible()
    for figure in (relief, surface, slope):
        figure.canvas.draw()
    assert plots.surface_exaggeration(np.array([[0.0, 2.0]]), (0, 100, 0, 50)) == pytest.approx(17.5)
