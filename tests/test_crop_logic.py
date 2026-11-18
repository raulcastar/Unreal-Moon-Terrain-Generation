"""Cropping a DEM before it goes to Unreal (gui.crop_logic) and NoData handling (gui.dem_files)."""

import json
import os

import numpy as np
import pytest

import heightmap_files
from gui import crop_logic as crop
from gui import paths
from gui.dem_files import load_dem_array


def test_landscape_sizes_need_no_padding():
    # Every listed size is built exactly: its layout has no edge padding.
    for size in crop.LANDSCAPE_SIZES[::7]:
        layout = heightmap_files.calculate_optimal_landscape_config(size, size)
        assert layout is not None and layout[4] == size and layout[5] == size
    assert crop.MIN_CROP == 64


@pytest.mark.parametrize("size, snapped", [(4097, 4081), (4081, 4081), (1000, 946), (64, 64), (10, 64)])
def test_snap_size(size, snapped):
    assert crop.snap_size(size) == snapped
    assert crop.snap_size(size) in crop.LANDSCAPE_SIZES


def test_clamp_keeps_the_crop_inside_the_map():
    assert crop.clamp_bounds(-50, -10, 200, 300, (1000, 1000)) == (0, 0, 200, 300)
    assert crop.clamp_bounds(950, 900, 200, 300, (1000, 1000)) == (800, 700, 200, 300)
    # Never smaller than one component, never larger than the map.
    assert crop.clamp_bounds(10, 10, 5, 5, (1000, 1000))[2:] == (64, 64)
    assert crop.clamp_bounds(0, 0, 5000, 5000, (1000, 800))[2:] == (1000, 800)
    # A map smaller than one component is cropped to the map.
    assert crop.clamp_bounds(0, 0, 10, 10, (30, 40)) == (0, 0, 30, 40)


def test_display_and_bounds_round_trip():
    shape = (2000, 3000)
    bounds = (300, 600, 900, 1200)
    x0, x1, y0, y1 = crop.bounds_to_display(bounds)
    assert crop.display_to_bounds(x0, x1, y0, y1, shape) == bounds
    # Drawn right-to-left / bottom-to-top gives the same rectangle.
    assert crop.display_to_bounds(x1, x0, y1, y0, shape) == bounds
    # Fractional coordinates round to whole pixels; a rectangle past the edge is moved inside.
    assert crop.display_to_bounds(-20.4, 150.6, 1990.2, 1800.7, shape) == (1801, 0, 190, 171)


@pytest.mark.parametrize("rows, cols", [(766, 1072), (700, 1100), (2100, 1100), (4097, 300), (5000, 12000)])
def test_snapped_crops_are_built_without_padding(rows, cols):
    # Both edges share one component size, so Unreal pads neither side.
    snapped_rows, snapped_cols = crop.snap_pair(rows, cols)
    assert snapped_rows <= rows and snapped_cols <= cols
    layout = heightmap_files.calculate_optimal_landscape_config(snapped_cols, snapped_rows)
    assert (layout[4], layout[5]) == (snapped_cols, snapped_rows)


def test_snap_bounds_keeps_the_centre_and_stays_inside():
    shape = (5000, 5000)
    row, col, rows, cols = crop.snap_bounds((1000, 1000, 2100, 1100), shape)
    assert rows in crop.LANDSCAPE_SIZES and cols in crop.LANDSCAPE_SIZES
    assert rows <= 2100 and cols <= 1100
    assert abs((row + rows / 2) - 2050) <= 1 and abs((col + cols / 2) - 1550) <= 1
    edge = crop.snap_bounds((4900, 4900, 600, 600), shape)
    assert edge[0] + edge[2] <= 5000 and edge[1] + edge[3] <= 5000


def test_default_crop():
    assert crop.default_crop((2000, 3000)) == (0, 0, 2000, 3000)    # small: whole map
    row, col, rows, cols = crop.default_crop((12060, 11660))       # LRO-sized: centred square
    assert rows == cols == 4081
    # Centred to the nearest pixel (an odd margin cannot split evenly).
    assert abs(row - (12060 - 4081) / 2) <= 1 and abs(col - (11660 - 4081) / 2) <= 1


def test_describe_crop():
    shape = (12060, 11660)
    whole = crop.describe_crop(crop.whole_map(shape), 1.0, shape)
    assert whole["whole"] and whole["fits"] and not whole["auto_import"]
    assert any("manual import" in line for line in whole["lines"])

    part = crop.describe_crop((0, 0, 4081, 4081), 0.5, shape)
    assert part["auto_import"] and part["padding"] == (0, 0)
    assert part["width_m"] == pytest.approx(2040.5)

    huge = crop.describe_crop((0, 0, 30000, 30000), 1.0)
    assert not huge["fits"] and huge["layout"] is None


def test_save_crop_from_npy(tmp_path):
    source = tmp_path / "big_elevationMap.npy"
    data = np.arange(300 * 400, dtype=np.float32).reshape(300, 400)
    np.save(source, data)

    bounds = (50, 100, 128, 190)
    dem_path, info = crop.save_crop(str(source), bounds, resolution=0.5)

    # Written under the (redirected) outputs folder, in its own map folder.
    assert os.path.commonpath([dem_path, paths.OUTPUT_ROOT]) == paths.OUTPUT_ROOT
    assert os.path.basename(dem_path) == "big_crop_x100_y50_190x128.npy"
    np.testing.assert_array_equal(np.load(dem_path), data[50:178, 100:290])
    assert info["resolution_m_per_px"] == 0.5 and info["missing_fraction"] == 0

    # The info is found from the crop, and gives its resolution back.
    assert crop.read_crop_info(dem_path)["x"] == 100
    assert crop.native_resolution(dem_path) == 0.5
    with open(crop.crop_paths(str(source), bounds)[2], encoding="utf-8") as handle:
        assert json.load(handle)["source"] == os.path.abspath(source)


def test_save_crop_refuses_an_empty_region(tmp_path):
    source = tmp_path / "holes.npy"
    data = np.ones((200, 200), dtype=np.float32)
    data[:100, :100] = np.nan
    np.save(source, data)
    with pytest.raises(ValueError, match="no elevation data"):
        crop.save_crop(str(source), (0, 0, 64, 64))


tifffile = pytest.importorskip("tifffile")


def _geotiff(path, data, nodata="-3.40282265508890445e+38", scale=1.0):
    """A minimal GeoTIFF: pixel scale and GDAL NoData tags, as an LRO DEM has."""
    tifffile.imwrite(path, data, extratags=[
        (33550, "d", 3, (scale, scale, 0.0), True),   # ModelPixelScaleTag
        (42113, "s", 0, nodata, True),                # GDAL_NODATA
    ])


def test_geotiff_nodata_becomes_nan_and_crops_keep_the_resolution(tmp_path):
    data = np.full((150, 160), 100.0, dtype=np.float32)
    data[:10, :] = np.float32(-3.4028227e38)       # LRO's NoData sentinel
    source = tmp_path / "lro.tif"
    _geotiff(source, data, scale=2.0)

    loaded = load_dem_array(str(source))
    assert np.isnan(loaded[:10]).all() and np.nanmin(loaded) == 100.0

    assert crop.native_resolution(str(source)) == 2.0
    dem_path, info = crop.save_crop(str(source), (0, 0, 64, 64))
    assert info["resolution_m_per_px"] == 2.0
    assert info["missing_fraction"] == pytest.approx(10 / 64)
    assert np.isnan(np.load(dem_path)[:10]).all()


def test_nanite_warning_only_for_large_landscapes():
    assert crop.nanite_warning((2017, 2017)) is None
    assert crop.nanite_warning((1009, 1261)) is None
    message = crop.nanite_warning((2080, 2269))      # rows, cols
    assert message is not None and "2269 x 2080" in message


def test_native_resolution_of_a_generated_map(tmp_path):
    # The generator's crater list gives each crater in pixels and metres.
    dem = tmp_path / "mare_elevationMap.npy"
    np.save(dem, np.zeros((8, 8), dtype=np.float32))
    (tmp_path / "mare_craters.csv").write_text(
        "id,row,col,radius_px,x_m,y_m,radius_m\n"
        "0,1,1,2.0,0.5,0.5,1.0\n"
        "1,4,4,40.0,2.0,2.0,20.0\n", encoding="utf-8")
    assert crop.native_resolution(str(dem)) == 0.5
    assert crop.native_resolution(str(tmp_path / "other.npy")) is None
