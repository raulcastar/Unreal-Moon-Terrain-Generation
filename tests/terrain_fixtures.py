"""Small synthetic DEMs with craters at known places, shared by the tests."""

import numpy as np

import terrain_core

# (row, col, radius) of the craters stamped into crater_dem(), in DEM cells.
KNOWN_CRATERS = [(60.0, 60.0, 14.0), (140.0, 70.0, 10.0), (100.0, 150.0, 18.0)]


def crater_dem(size=200, craters=KNOWN_CRATERS, depth_ratio=0.25, rim_ratio=0.3,
               slope=0.01, noise=0.005, seed=3):
    """A gently sloping plain with the given craters (generator's crater profile)."""
    rng = np.random.default_rng(seed)
    dem = slope * np.arange(size, dtype=float)[None, :] + np.zeros((size, size))
    for row, col, radius in craters:
        terrain_core.add_crater(dem, int(row), int(col), radius, radius * depth_ratio, rim_ratio)
    return dem + rng.normal(0.0, noise, dem.shape)
