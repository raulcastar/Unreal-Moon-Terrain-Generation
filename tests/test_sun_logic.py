"""Sun & Shadows logic (gui.sun_logic) and drawing (gui.sun_plots)."""

import math

import numpy as np
import pytest
from matplotlib.figure import Figure

import sun_shadows
from gui import sun_logic as logic
from gui import sun_plots as plots


def test_clamp_sun():
    assert logic.clamp_sun(370.0, 95.0) == (10.0, 90.0)
    assert logic.clamp_sun(-30.0, 0.0) == (330.0, logic.MIN_ELEVATION_DEG)


def test_parse_positive():
    assert logic.parse_positive("2", 1.0) == 2.0
    assert logic.parse_positive("0", 1.0) == 1e-3 and logic.parse_positive("x", 1.0) == 1.0


def test_shadow_direction_and_light_rotation():
    assert logic.shadow_direction(300.0) == 120.0
    pitch, yaw = logic.unreal_light_rotation(90.0, 20.0)       # sun in the east
    assert pitch == -20.0 and yaw == pytest.approx(0.0, abs=1e-9)
    assert logic.unreal_light_rotation(0.0, 20.0)[1] == pytest.approx(90.0)


def test_sun_info_text():
    text = logic.sun_info_text(90.0, 30.0, 1.0, 1.73)
    assert text.startswith("Light from 90.0 deg, 30.0 deg up: a 1 m object casts a 1.73 m shadow")
    assert "towards 270 deg" in text and "pitch -30.0, yaw 0.0" in text


def test_sun_from_shadow_fields_round_trip():
    azimuth, elevation = 135.0, 20.0
    length = sun_shadows.shadow_length(2.0, elevation)
    sun = logic.sun_from_shadow_fields(str(logic.shadow_direction(azimuth)), str(length), 2.0)
    assert sun[0] % 360 == pytest.approx(azimuth) and sun[1] == pytest.approx(elevation)
    assert logic.sun_from_shadow_fields("a", "1", 1.0) == "Shadow direction and length must be numbers."
    assert logic.sun_from_shadow_fields("10", "0", 1.0) == "The shadow length must be positive."


def test_probe_tip_points_away_from_the_sun():
    assert logic.probe_tip(0.0, 0.0, 0.0, 2.0) == pytest.approx((0.0, 2.0))      # sun north
    assert logic.probe_tip(0.0, 0.0, 90.0, 2.0) == pytest.approx((-2.0, 0.0))    # sun east


def test_parse_sun_settings_and_log():
    assert logic.parse_sun_settings("120000", "0.53") == (120000.0, 0.53)
    assert "numbers" in logic.parse_sun_settings("x", "1")
    assert "positive" in logic.parse_sun_settings("0", "1")
    line = logic.unreal_sun_log({"label": "Sun", "azimuth": 10, "elevation": 5, "intensity": 1e5,
                                 "pie": True})
    assert line == "Read 'Sun' from the running game: azimuth 10.0, elevation 5.0 deg, 100000 lux."


def test_reduce_dem():
    dem = np.zeros((3000, 1500))
    reduced, res = logic.reduce_dem(dem, 1000, 0.5)
    assert reduced.shape == (1000, 500) and res == 1.5
    assert logic.reduce_dem(np.zeros((100, 80)), 1000, 2.0)[1] == 2.0


def test_lunar_tone_and_display():
    light = np.linspace(0.0, 1.0, 101).reshape(1, -1)
    tone = logic.lunar_tone(light)
    assert tone[0, 0] == 0.0 and np.all(np.diff(tone[0]) >= 0)
    median_lit = float(np.median(light[light > 0]))
    assert np.interp(median_lit, light[0], tone[0]) == pytest.approx(logic.SUN_TONE_GROUND, abs=0.01)
    shadow = np.zeros_like(light, bool)
    shadow[0, :10] = True
    relief = np.full_like(light, 0.5)
    plain = logic.display_rgb(light, shadow, relief, logic.SUN_DISPLAY_MODES[0])
    tinted = logic.display_rgb(light, shadow, relief, logic.SUN_DISPLAY_MODES[1])
    over_relief = logic.display_rgb(light, shadow, relief, logic.SUN_DISPLAY_MODES[2])
    assert plain.shape == (1, 101, 3) and np.array_equal(plain[0, 50:], tinted[0, 50:])
    assert not np.array_equal(plain[0, :10], tinted[0, :10])
    assert np.allclose(over_relief[0, 50:], 0.5)


def test_relief_and_shadow_shares():
    dem = np.random.default_rng(0).normal(size=(30, 40)).cumsum(axis=0)
    relief = logic.relief_shading(dem)
    assert relief.min() >= 0.25 - 1e-9 and relief.max() <= 0.85 + 1e-9
    dem[0, 0] = np.nan
    shadow = np.zeros(dem.shape, bool)
    shadow[:3] = True
    light = np.ones(dem.shape)
    light[-3:] = 0.0
    cast, dark = logic.shadow_shares(dem, shadow, light)
    assert cast == pytest.approx(100 * (3 * 40 - 1) / (30 * 40 - 1))
    assert dark == pytest.approx(100 * 3 * 40 / (30 * 40 - 1))


def test_readouts():
    assert logic.map_title(10.0, 20.0, 5.0) == "Sun from 10.0 deg at 20.0 deg  -  5.0% of the map in cast shadow"
    text = logic.readout_text(90.0, 45.0, 5.0, 2.0, (100, 200), 0.5)
    assert "X -1.00" in text and "length    1.00 x height" in text and "Preview 200 x 100 px" in text


def test_map_and_3d_drawing():
    dem = np.random.default_rng(1).normal(size=(40, 60)).cumsum(axis=1)
    figure = Figure()
    artists = plots.build_map(figure, dem.shape, 2.0, unreal_view=True)
    assert artists["axes"].xaxis_inverted()
    plots.place_sun(artists, 90.0, 30.0)
    assert artists["sun_dot"].get_data()[1][0] == pytest.approx(60.0)
    plots.draw_probe(artists["axes"], 10.0, 10.0, (14.0, 13.0), 1.0)
    figure.canvas.draw()
    rgb = np.full(dem.shape + (3,), 0.5)
    figure3d = Figure()
    plots.draw_3d(figure3d, dem, 2.0, rgb, 90.0, 30.0)
    figure3d.canvas.draw()
    assert figure3d.axes[0].get_title().startswith("Sun from 90.0 deg at 30.0 deg")
    assert math.isfinite(figure3d.axes[0].get_zlim()[1])
