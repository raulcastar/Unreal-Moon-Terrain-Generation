"""
Sun Position & Cast Shadows
---------------------------
Sun geometry over a DEM: the direction towards the sun, the sun position that
throws a requested shadow, direct illumination and cast-shadow masks. UI-free.

Conventions follow the DEM image (row 0 at the top):
    azimuth    degrees clockwise from the top (decreasing rows); 90 = +columns
    elevation  degrees above the horizon
Vectors are in (column, row, up) axes, in metres.
"""

import math

import numpy as np
from scipy import ndimage


def sun_vector(azimuth_deg, elevation_deg):
    """Unit vector towards the sun in (column, row, up) axes."""
    a, e = math.radians(azimuth_deg), math.radians(elevation_deg)
    return np.array([math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)])


def sun_from_shadow(d_col_m, d_row_m, height_m):
    """
    Sun (azimuth, elevation) in degrees that makes an object `height_m` tall
    cast its shadow from its base to the point (d_col_m, d_row_m) metres away.
    The shadow points away from the sun; its length sets the elevation.
    """
    length = math.hypot(d_col_m, d_row_m)
    if length < 1e-9:
        return 0.0, 90.0
    azimuth = math.degrees(math.atan2(-d_col_m, d_row_m)) % 360.0
    elevation = math.degrees(math.atan2(max(height_m, 0.0), length))
    return azimuth, elevation


def shadow_length(height_m, elevation_deg):
    """Length (m) of the shadow of an object `height_m` tall on flat ground."""
    if elevation_deg >= 90.0:
        return 0.0
    if elevation_deg <= 0.0:
        return math.inf
    return height_m / math.tan(math.radians(elevation_deg))


def cast_shadows(dem, resolution, azimuth_deg, elevation_deg):
    """
    Boolean mask of the cells the terrain hides from the sun.

    The DEM is resampled onto a grid aligned with the light, then swept once
    along it, carrying the top of the shadow cast upstream (falling by
    resolution * tan(elevation) per cell). A cell is shadowed when that top is
    above it. Each map cell then takes its nearest grid sample.
    """
    dem = np.asarray(dem, dtype=float)
    if elevation_deg >= 90.0:
        return np.zeros(dem.shape, dtype=bool)
    if elevation_deg <= 0.0:
        return np.ones(dem.shape, dtype=bool)

    # Sweep grid: u along the light's travel, v across it, both centred on the map.
    a = math.radians(azimuth_deg)
    travel = np.array([-math.sin(a), math.cos(a)])        # (d_col, d_row)
    across = np.array([-travel[1], travel[0]])
    h, w = dem.shape
    centre = np.array([(w - 1) / 2.0, (h - 1) / 2.0])
    n = int(math.ceil(math.hypot(h, w))) + 2
    half = (n - 1) / 2.0
    vv, uu = np.mgrid[0:n, 0:n] - half
    cols = centre[0] + uu * travel[0] + vv * across[0]
    rows = centre[1] + uu * travel[1] + vv * across[1]
    rot = ndimage.map_coordinates(dem, (rows, cols), order=1, mode="nearest")
    outside = (cols < -0.5) | (cols > w - 0.5) | (rows < -0.5) | (rows > h - 0.5)
    rot[outside] = np.nan

    drop = resolution * math.tan(math.radians(elevation_deg))
    eps = 1e-6 * max(1.0, float(np.nanmax(np.abs(dem))))   # Avoids self-shadowing on flat ground
    shadow = np.zeros(rot.shape, dtype=bool)
    horizon = np.full(n, -np.inf)
    for j in range(n):
        known = np.isfinite(rot[:, j])
        column = np.where(known, rot[:, j], -np.inf)
        horizon -= drop
        shadow[:, j] = known & (horizon > column + eps)
        np.maximum(horizon, column, out=horizon)

    # Back to map cells: nearest sweep-grid sample of each cell.
    r, c = np.mgrid[0:h, 0:w]
    dc, dr = c - centre[0], r - centre[1]
    u = np.rint(dc * travel[0] + dr * travel[1] + half).astype(int)
    v = np.rint(dc * across[0] + dr * across[1] + half).astype(int)
    return shadow[v, u] & np.isfinite(dem)


def illumination(dem, resolution, azimuth_deg, elevation_deg, shadow=None):
    """
    Direct sunlight on each cell, 0..1: the cosine of the incidence angle on
    the local slope, zero on slopes facing away and, when given, in cast shadow.
    """
    d_row, d_col = np.gradient(np.asarray(dem, dtype=float), resolution)
    normal = np.stack((-d_col, -d_row, np.ones_like(d_col)))
    normal /= np.linalg.norm(normal, axis=0)
    light = np.clip(np.tensordot(sun_vector(azimuth_deg, elevation_deg), normal, axes=1), 0.0, 1.0)
    if shadow is not None:
        light = np.where(shadow, 0.0, light)
    return light
