"""terrain_core: parameters, the crater profile, generation and export."""

import numpy as np
import pandas as pd
import pytest

import crater_map
import terrain_core


def small_params(**overrides):
    params = terrain_core.coerce_params({"ROWS": 160, "COLS": 200, "RANDOM_SEED": 7,
                                         "CRATER_DENSITY": 0.0005})
    params.update(overrides)
    return params


# ---------------------------------------------------------------- parameters

def test_coerce_params_types_and_defaults():
    params = terrain_core.coerce_params({"ROWS": "300", "MICRO_ROUGHNESS": "0.1",
                                         "RANDOM_SEED": "12", "MY_NOTE": "kept"})
    assert params["ROWS"] == 300 and isinstance(params["ROWS"], int)
    assert params["MICRO_ROUGHNESS"] == pytest.approx(0.1)
    assert params["RANDOM_SEED"] == 12
    assert params["MY_NOTE"] == "kept"                   # unknown keys survive
    assert params["COLS"] == terrain_core.DEFAULTS["COLS"]


@pytest.mark.parametrize("seed", [None, "", "null"])
def test_coerce_params_empty_seed_is_none(seed):
    assert terrain_core.coerce_params({"RANDOM_SEED": seed})["RANDOM_SEED"] is None


def test_config_round_trip(tmp_path):
    path = tmp_path / "preset.yaml"
    params = small_params(MY_NOTE="kept")
    terrain_core.save_config(str(path), params)
    assert terrain_core.load_config(str(path)) == terrain_core.coerce_params(params)


# ---------------------------------------------------------------- crater profile

def test_add_crater_profile():
    """Bowl floor at -depth, rim crest at the radius, ejecta gone at 1.5 x radius."""
    size, radius, depth, rim = 101, 20.0, 5.0, 0.3
    dem = np.zeros((size, size))
    terrain_core.add_crater(dem, 50, 50, radius, depth, rim)
    assert dem[50, 50] == pytest.approx(-depth)
    assert dem[50, 50 + 20] == pytest.approx(depth * rim)          # crest
    assert dem[50, 50 + 30] == pytest.approx(0.0, abs=1e-9)         # end of the ejecta
    assert dem[0, 0] == 0.0 and dem[50, 0] == 0.0                   # untouched outside
    # The wall is the parabola from the floor to the crest ...
    for d in (5, 10, 15, 19):
        assert dem[50, 50 + d] == pytest.approx(-depth + (depth + rim * depth) * (d / radius) ** 2)
    # ... so the profile climbs steadily to the crest and falls away after it,
    # with no cliff at the crater edge.
    profile = dem[50, 50:81]
    assert np.all(np.diff(profile[:21]) > 0) and np.all(np.diff(profile[20:]) <= 0)
    assert profile.argmax() == 20


def test_add_crater_off_map_is_ignored():
    dem = np.zeros((50, 50))
    terrain_core.add_crater(dem, -500, -500, 10.0, 2.0, 0.3)
    assert not dem.any()


# ---------------------------------------------------------------- generation

def test_generate_terrain_is_deterministic():
    a = terrain_core.generate_terrain(small_params())
    b = terrain_core.generate_terrain(small_params())
    assert np.array_equal(a["elevation"], b["elevation"])
    assert np.array_equal(a["craters"], b["craters"])
    c = terrain_core.generate_terrain(small_params(), seed=8)
    assert not np.array_equal(a["elevation"], c["elevation"])


def test_generate_terrain_shape_and_values():
    result = terrain_core.generate_terrain(small_params())
    elevation = result["elevation"]
    assert elevation.shape == (160, 200) == (result["rows"], result["cols"])
    assert np.isfinite(elevation).all()
    # Around the base elevation, within the macro range plus crater relief.
    assert abs(float(np.median(elevation)) - terrain_core.DEFAULTS["BASE_ELEVATION"]) < 60


def test_craters_do_not_overlap_and_respect_radius_bounds():
    params = small_params()
    craters = terrain_core.generate_terrain(params)["craters"]
    assert len(craters) > 3
    longest = max(params["ROWS"], params["COLS"])
    radii = craters[:, 2]
    assert radii.min() >= longest * params["CRATER_MIN_RADIUS_PCT"] - 1e-9
    assert radii.max() <= longest * params["CRATER_MAX_RADIUS_PCT"] + 1e-9
    for i in range(len(craters)):
        for j in range(i + 1, len(craters)):
            gap = np.hypot(*(craters[i, :2] - craters[j, :2]))
            assert gap >= 1.5 * (radii[i] + radii[j]) - 1e-9, "craters overlap, rims included"


def test_explicit_crater_count_is_used():
    result = terrain_core.generate_terrain(small_params(), crater_count=2)
    assert result["crater_count"] <= 2


def test_cancel_raises():
    with pytest.raises(terrain_core.TerrainCancelled):
        terrain_core.generate_terrain(small_params(), cancel_check=lambda: True)


# ---------------------------------------------------------------- export

def test_export_outputs_and_crater_list_round_trip(outputs_dir):
    params = small_params()
    result = terrain_core.generate_terrain(params)
    written = terrain_core.export_outputs(result["elevation"], str(outputs_dir),
                                          base_name="unit_elevationMap",
                                          craters=result["craters"], params=params)
    assert written, "nothing written"
    for path in written.values():
        assert str(outputs_dir) in path, f"{path} written outside the test folder"
    npy = next(p for p in written.values() if p.endswith(".npy"))
    assert np.allclose(np.load(npy), result["elevation"])
    crater_csv = next(p for p in written.values() if p.endswith("_craters.csv"))
    loaded = crater_map.load_crater_list(crater_csv)
    assert np.allclose(np.array(loaded), result["craters"], atol=1e-3)
    frame = pd.read_csv(crater_csv)
    assert np.allclose(frame["depth_m"], frame["radius_px"] * params["CRATER_DEPTH_RATIO"]
                       * params["RESOLUTION_M_PER_PX"], atol=1e-3)


def test_full_crater_count():
    params = small_params()
    assert terrain_core.full_crater_count(params) == int(160 * 200 * params["CRATER_DENSITY"])
