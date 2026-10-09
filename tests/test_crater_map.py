"""crater_map: crater detection, the label map and its export."""

import json

import numpy as np
import pytest
from PIL import Image

import crater_map
import mask_classes
from terrain_fixtures import KNOWN_CRATERS, crater_dem

RESOLUTION = 1.0


def test_ids_come_from_mask_classes():
    assert crater_map.CRATER_ID == mask_classes.id_of("crater")
    assert crater_map.RIM_ID == mask_classes.id_of("crater_rim")


# ---------------------------------------------------------------- detection

def test_finds_the_stamped_craters():
    found = crater_map.find_craters(crater_dem(), RESOLUTION, 0.5)
    assert len(found) == len(KNOWN_CRATERS)
    for row, col, radius in KNOWN_CRATERS:
        best = min(found, key=lambda c: np.hypot(c[0] - row, c[1] - col))
        assert np.hypot(best[0] - row, best[1] - col) < 1.5, "centre off"
        assert best[2] == pytest.approx(radius, rel=0.15), "radius off"


def test_shallow_basins_are_not_craters():
    """A bowl far shallower than MIN_DEPTH_RATIO x diameter is not a crater."""
    shallow = crater_dem(craters=[(100.0, 100.0, 20.0)], depth_ratio=0.01, rim_ratio=0.0)
    assert crater_map.find_craters(shallow, RESOLUTION, 0.05) == []


def test_flat_map_has_no_craters():
    assert crater_map.find_craters(np.zeros((60, 60)), RESOLUTION, 0.5) == []


# ---------------------------------------------------------------- labels

def test_render_labels_crater_rim_and_ground():
    labels = crater_map.render_labels((100, 100), [(50.0, 50.0, 10.0)])
    assert labels.dtype == np.uint8 and labels.shape == (100, 100)
    assert labels[50, 50] == crater_map.CRATER_ID
    assert labels[50, 50 + 11] == crater_map.RIM_ID          # inside 1.2 x radius
    assert labels[50, 50 + 13] == 0
    assert labels[0, 0] == 0


def test_render_labels_supersampled():
    labels = crater_map.render_labels((40, 50), [(20.0, 25.0, 5.0)], supersample=4)
    assert labels.shape == (160, 200)
    assert labels[20 * 4 + 1, 25 * 4 + 1] == crater_map.CRATER_ID


def test_render_labels_without_rims():
    labels = crater_map.render_labels((60, 60), [(30.0, 30.0, 8.0)], rim_id=0)
    assert set(np.unique(labels)) == {0, crater_map.CRATER_ID}


def test_craters_win_over_neighbouring_rims():
    """Where one crater's rim ring crosses another crater, the crater id stays."""
    labels = crater_map.render_labels((100, 100), [(50.0, 40.0, 10.0), (50.0, 61.0, 10.0)])
    assert labels[50, 51] == crater_map.CRATER_ID           # in crater 2, in crater 1's ring


def test_crater_labels_blank_where_the_dem_has_no_data():
    dem = crater_dem()
    dem[95:105, 95:105] = np.nan
    labels, count = crater_map.crater_labels(dem, RESOLUTION, 0.5)
    assert count >= 1
    assert not labels[95:105, 95:105].any()


def test_supersample_for_keeps_the_map_within_the_texture_limit():
    assert crater_map.supersample_for((100, 100)) == crater_map.SUPERSAMPLE
    big = crater_map.MAX_MAP_SIDE
    assert crater_map.supersample_for((big, 10)) == 1
    assert crater_map.supersample_for((big // 2, 10)) * (big // 2) <= big


# ---------------------------------------------------------------- export

def test_export_crater_mask(outputs_dir):
    labels = crater_map.render_labels((64, 80), [(32.0, 40.0, 9.0)])
    written = crater_map.export_crater_mask(labels, str(outputs_dir), "unit_elevationMap",
                                            RESOLUTION, 0.5)
    for path in written.values():
        assert str(outputs_dir) in path, f"{path} written outside the test folder"
    assert np.array_equal(np.array(Image.open(written["crater_mask_png"])), labels)
    meta = json.load(open(written["crater_mask_json"]))
    assert (meta["width"], meta["height"]) == (80, 64)
    assert (meta["crater_id"], meta["rim_id"]) == (crater_map.CRATER_ID, crater_map.RIM_ID)
    assert meta["crater_cover"] == pytest.approx((labels == crater_map.CRATER_ID).mean())
