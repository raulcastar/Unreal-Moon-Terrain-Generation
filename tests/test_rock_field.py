"""rock_field: the rock-size law, placement rules and export."""

import numpy as np
import pandas as pd
import pytest
from scipy import ndimage

import crater_map
import rock_field
from terrain_fixtures import crater_dem

RESOLUTION = 1.0


@pytest.fixture(scope="module")
def dem():
    return crater_dem()


@pytest.fixture(scope="module")
def field(dem):
    params = rock_field.coerce_params({"ROCK_COVERAGE": 0.03, "ROCK_MIN_SCALE": 0.3,
                                       "ROCK_MAX_SCALE": 2.5, "PATH_WIDTH_M": 4.0,
                                       "MIN_CRATER_DEPTH_M": 0.5})
    return rock_field.build_rock_field(dem, RESOLUTION, params, seed=11)


# ---------------------------------------------------------------- size law

def test_sample_radius_bounds_and_monotonic():
    u = np.linspace(0.0, 0.999999, 1000)
    radii = np.array([rock_field._sample_radius(x, 0.3, 2.5) for x in u])
    assert radii[0] == pytest.approx(0.3)
    assert radii.max() <= 2.5 + 1e-9
    assert np.all(np.diff(radii) >= 0)
    assert rock_field._sample_radius(0.7, 1.0, 1.0) == 1.0     # degenerate range


def test_rock_sizes_follow_the_power_law():
    """Share of rocks larger than r matches the truncated power law (k = 2.5)."""
    rng = np.random.default_rng(0)
    lo, hi, k = 0.3, 2.5, rock_field.ROCK_SIZE_EXPONENT
    radii = np.array([rock_field._sample_radius(u, lo, hi) for u in rng.random(200_000)])
    for r in (0.4, 0.6, 1.0, 1.5, 2.0):
        expected = (r ** -k - hi ** -k) / (lo ** -k - hi ** -k)
        assert np.mean(radii > r) == pytest.approx(expected, abs=0.005)
    # Most rocks near the minimum, large boulders rare.
    assert np.median(radii) < 0.45 and np.mean(radii > 2.0) < 0.01
    # The analytic mean footprint used for the rock count matches the samples.
    assert rock_field._mean_square_radius(lo, hi) == pytest.approx(np.mean(radii ** 2), rel=0.02)


def test_estimate_rock_count_scales_with_area_and_coverage():
    params = rock_field.coerce_params({})
    one = rock_field.estimate_rock_count(params, 10_000.0)
    assert rock_field.estimate_rock_count(params, 20_000.0) == pytest.approx(2 * one, abs=1)
    denser = rock_field.coerce_params({"ROCK_COVERAGE": 2 * params["ROCK_COVERAGE"]})
    assert rock_field.estimate_rock_count(denser, 10_000.0) == pytest.approx(2 * one, abs=1)


def test_coerce_params_keeps_max_scale_at_least_min():
    params = rock_field.coerce_params({"ROCK_MIN_SCALE": 1.0, "ROCK_MAX_SCALE": 0.5})
    assert params["ROCK_MAX_SCALE"] == 1.0


# ---------------------------------------------------------------- placement rules

def test_field_has_rocks_and_corridors(field):
    assert field["plan_shape"] == (200, 200)               # small DEM: planned at full size
    assert field["rock_count"] > 100
    assert field["corridor"].any()
    assert field["connected"]


def test_no_rock_inside_a_crater(field, dem):
    """Every rock footprint stays outside every crater circle and the deep crater cells."""
    rocks = field["rocks_px"]
    craters = crater_map.find_craters(dem, RESOLUTION, field["params"]["MIN_CRATER_DEPTH_M"])
    assert len(craters) >= 3
    for row, col, radius in craters:
        gaps = np.hypot(rocks[:, 0] - row, rocks[:, 1] - col)
        assert np.all(gaps >= radius + rocks[:, 2]), "a rock overlaps a crater"
    cells = np.round(rocks[:, :2]).astype(int)
    assert not field["crater_mask"][cells[:, 0], cells[:, 1]].any()


def test_rocks_do_not_overlap(field):
    rocks = field["rocks_px"]
    for i in range(len(rocks) - 1):
        gaps = np.hypot(rocks[i + 1:, 0] - rocks[i, 0], rocks[i + 1:, 1] - rocks[i, 1])
        assert np.all(gaps >= rocks[i + 1:, 2] + rocks[i, 2] - 1e-9), f"rock {i} overlaps another"


def test_rocks_keep_the_rover_corridor_clear(field):
    rocks = field["rocks_px"]
    half_width_px = 0.5 * field["params"]["PATH_WIDTH_M"] / RESOLUTION
    distance = ndimage.distance_transform_edt(~field["corridor"])
    cells = np.round(rocks[:, :2]).astype(int)
    assert np.all(distance[cells[:, 0], cells[:, 1]] > rocks[:, 2] + half_width_px)


def test_rock_scale_and_height_cap(field):
    """Scales within the configured bounds; height = scale x ROCK_HEIGHT_RATIO, never above the cap."""
    params = field["params"]
    world = field["rocks_world"]
    scale, height = world[:, 4], world[:, 5]
    assert scale.min() >= params["ROCK_MIN_SCALE"] - 1e-9
    assert scale.max() <= params["ROCK_MAX_SCALE"] + 1e-9
    assert np.allclose(height, scale * params["ROCK_HEIGHT_RATIO"])
    assert height.max() <= params["ROCK_MAX_SCALE"] * params["ROCK_HEIGHT_RATIO"] + 1e-9


def test_rock_coverage_close_to_target(field):
    rocks = field["rocks_world"]
    drivable_m2 = field["region"].sum() * RESOLUTION ** 2
    covered = np.sum(np.pi * rocks[:, 4] ** 2) / drivable_m2
    assert field["rock_count"] <= field["rock_requested"]
    assert covered == pytest.approx(field["params"]["ROCK_COVERAGE"], rel=0.35)


def test_world_coordinates_match_the_plan(field):
    rocks, world = field["rocks_px"], field["rocks_world"]
    assert np.allclose(world[:, 2], rocks[:, 1] * RESOLUTION)     # x east = column
    assert np.allclose(world[:, 3], rocks[:, 0] * RESOLUTION)     # y north = row


def test_same_seed_same_field(dem, field):
    again = rock_field.build_rock_field(dem, RESOLUTION, field["params"], seed=11)
    assert np.array_equal(again["rocks_px"], field["rocks_px"])
    other = rock_field.build_rock_field(dem, RESOLUTION, field["params"], seed=12)
    assert not np.array_equal(other["rocks_px"], field["rocks_px"])


def test_max_rocks_cap(dem, monkeypatch):
    monkeypatch.setattr(rock_field, "MAX_ROCKS", 40)
    params = rock_field.coerce_params({"ROCK_COVERAGE": 0.2, "ROCK_MIN_SCALE": 0.3,
                                       "ROCK_MAX_SCALE": 0.4})
    result = rock_field.build_rock_field(dem, RESOLUTION, params, seed=1)
    assert result["rock_capped"] and result["rock_count"] <= 40


def test_flat_map_without_craters_has_no_corridors():
    flat = np.zeros((80, 80))
    result = rock_field.build_rock_field(flat, RESOLUTION, rock_field.coerce_params({}), seed=1)
    assert not result["crater_mask"].any() and not result["corridor"].any()
    assert result["rock_count"] > 0


# ---------------------------------------------------------------- export

def test_export_rock_field(field, outputs_dir):
    written = rock_field.export_rock_field(field, str(outputs_dir), "unit_elevationMap",
                                           export_obstacle_map=True)
    for path in written.values():
        assert str(outputs_dir) in path, f"{path} written outside the test folder"
    rocks = pd.read_csv(written["rocks_csv"])
    assert list(rocks.columns) == ["id", "row", "col", "x_m", "y_m", "scale", "height_m"]
    assert len(rocks) == field["rock_count"]
    assert np.allclose(rocks["scale"], field["rocks_world"][:, 4], atol=1e-4)
    obstacle = np.loadtxt(written["obstacle_csv"])
    assert obstacle.shape == field["full_shape"]
    paths = pd.read_csv(written["paths_csv"])
    assert set(paths["path_id"]) == set(range(len(field["paths"])))


# ---------------------------------------------------------------- the generator's craters

@pytest.fixture(scope="module")
def generated():
    """A small generated terrain and its exact crater list (row, col, radius, depth_m)."""
    import terrain_core
    params = terrain_core.coerce_params({**terrain_core.default_params(), "ROWS": 300, "COLS": 300})
    result = terrain_core.generate_terrain(params, seed=5)
    craters = [(r, c, rad, rad * params["CRATER_DEPTH_RATIO"]) for r, c, rad in result["craters"]]
    return result["elevation"], craters


def test_generator_craters_drive_the_plan(generated):
    dem, craters = generated
    params = rock_field.coerce_params({"MIN_CRATER_DEPTH_M": 0.5})
    field = rock_field.build_rock_field(dem, RESOLUTION, params, seed=3, craters=craters)
    assert field["crater_source"] == "generator" and field["crater_count"] == len(craters)
    # Impassable: exactly the bowls at least MIN_CRATER_DEPTH_M deep (plan grid = DEM here).
    deep = [c for c in craters if c[3] >= 0.5]
    expected = rock_field._crater_disks(dem.shape, deep, 1.0, 1.0)
    assert np.array_equal(field["crater_mask"], expected)
    # The corridors' sites are those craters (each its centre; a crater cut by the
    # map edge, the centre of the part on the map).
    for row, col in field["markers"]:
        assert any(np.hypot(r - row, c - col) <= radius for r, c, radius, _ in deep)
    assert field["road_segments"] > 0


def test_no_rock_inside_any_generator_crater(generated):
    """Every crater, however shallow, keeps its bowl (up to the rim crest) free of rocks."""
    dem, craters = generated
    params = rock_field.coerce_params({"MIN_CRATER_DEPTH_M": 5.0, "ROCK_COVERAGE": 0.05})
    field = rock_field.build_rock_field(dem, RESOLUTION, params, seed=3, craters=craters)
    assert field["rock_count"] > 0
    for row, col, *_ in field["rocks_px"]:
        for r, c, radius, _depth in craters:
            assert np.hypot(row - r, col - c) > radius
