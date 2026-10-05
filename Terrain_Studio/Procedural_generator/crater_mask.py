"""
Crater Ground Truth
-------------------
Full-resolution crater label map for the rover's object mask.

Craters are seeded by the closed depressions rock_field.detect_craters finds,
on the DEM and on the DEM with its regional relief removed (which separates
small craters lying in a wide basin). Each seed is fitted with the circle
through its rim crest, located along radial elevation profiles, so the crater
covers the whole bowl even on sloping ground; the rim is a ring just outside
the crest. Shallow or elongated basins (terrain lows rather than impact
craters) are rejected.

The map stores the mask ids themselves (0 = not a crater), so Unreal's mask
material can read a cell and use its value as the stencil id. Craters are
circles, so the map can be drawn finer than the DEM (SUPERSAMPLE cells per DEM
cell) for smooth edges up close. UI-free.
"""

import json
import os

import numpy as np
from PIL import Image
from scipy import ndimage

from rock_field import detect_craters

CRATER_ID = 30          # Must match mask_labels in component_stereo_depth/config/params.yaml
RIM_ID = 31
RIM_FRACTION = 0.2      # Rim width as a fraction of the crater radius
MIN_DEPTH_RATIO = 0.04  # Rim-to-floor depth / diameter below which a basin is not a crater
MIN_ROUNDNESS = 0.5     # Seed area / area of its enclosing circle
MIN_CREST_SHARE = 0.5   # Share of radial profiles that must show a rim crest
MAX_CREST_SPREAD = 0.12 # Spread of crest radii around the fitted circle, / radius
MAX_OVERLAP = 0.5       # Share of a new crater already covered by kept ones
RAYS = 48               # Radial profiles per crater
REGIONAL_SIGMA_M = 15.0 # Smoothing that defines the regional relief for the second pass
SUPERSAMPLE = 4         # Map cells per DEM cell, along each axis
MAX_MAP_SIDE = 8192     # Largest map side (texture limit)


def _rim_circle(dem, centre, radius):
    """
    Circle through the rim crest: the highest point of each radial profile,
    least-squares fitted, refined twice. Profiles whose highest point is at the
    search limit (no crest, e.g. uphill on a slope) are ignored. Returns
    (centre, radius, crest elevation), or None when no clear crest is found.
    """
    rows, cols = dem.shape
    angles = np.linspace(0.0, 2 * np.pi, RAYS, endpoint=False)
    found = None
    for _ in range(3):
        radii = np.arange(0.7 * radius, 1.6 * radius + 1.0, 0.5)
        rr = centre[0] + np.outer(np.sin(angles), radii)
        cc = centre[1] + np.outer(np.cos(angles), radii)
        inside = ((rr >= 0) & (rr <= rows - 1) & (cc >= 0) & (cc <= cols - 1)).all(axis=1)
        if inside.sum() < 6:
            break
        profiles = ndimage.map_coordinates(dem, (rr[inside], cc[inside]), order=1)
        peak = np.argmax(profiles, axis=1)
        crest = peak < len(radii) - 1
        if crest.sum() < max(6, MIN_CREST_SHARE * inside.sum()):
            break
        ys = rr[inside][crest, peak[crest]]
        xs = cc[inside][crest, peak[crest]]
        # Algebraic circle fit: x^2 + y^2 + D x + E y + F = 0.
        a = np.column_stack((xs, ys, np.ones_like(xs)))
        d, e, f = np.linalg.lstsq(a, -(xs ** 2 + ys ** 2), rcond=None)[0]
        fitted = (-e / 2, -d / 2)
        fitted_radius = np.sqrt(max(fitted[0] ** 2 + fitted[1] ** 2 - f, 0.0))
        if not 0.5 * radius < fitted_radius < 2.0 * radius:
            break
        spread = np.std(np.hypot(ys - fitted[0], xs - fitted[1])) / fitted_radius
        if spread > MAX_CREST_SPREAD:
            break
        centre, radius = fitted, fitted_radius
        found = (centre, radius, float(np.median(profiles[crest, peak[crest]])))
    return found


def _seeds(dem, resolution, min_depth_m, report):
    """
    (seed mask, depression) from the DEM, then from its local relief. Flood
    filling takes ~90% of each pass, so it spans 0-45% and 50-95% of the progress.
    """
    yield detect_craters(dem, min_depth_m, lambda f: report(
        "Finding depressions (pass 1 of 2)", 0.45 * f))
    local = dem - ndimage.gaussian_filter(dem, REGIONAL_SIGMA_M / resolution)
    yield detect_craters(local, min_depth_m, lambda f: report(
        "Finding depressions (pass 2 of 2)", 0.5 + 0.45 * f))


def supersample_for(shape):
    """Cells per DEM cell that keeps the map within MAX_MAP_SIDE."""
    return max(1, min(SUPERSAMPLE, MAX_MAP_SIDE // max(shape)))


def find_craters(dem, resolution, min_depth_m, progress=None):
    """
    Craters as [(row, col, radius)] in DEM cells: the circle through each rim
    crest, or the seed itself where no clear crest is found. `progress(message,
    fraction)` is called as the two passes advance.
    """
    def report(message, fraction):
        if progress:
            progress(message, fraction)

    dem = np.asarray(dem, dtype=float)
    finite = np.isfinite(dem)
    dem = np.where(finite, dem, np.nanmin(dem) if finite.any() else 0.0)

    covered = np.zeros(dem.shape, dtype=bool)
    craters = []
    for passed, (seeds, depression) in enumerate(_seeds(dem, resolution, min_depth_m, report)):
        blobs, count = ndimage.label(seeds, structure=np.ones((3, 3)))
        base = 0.5 * passed
        for index, window in enumerate(ndimage.find_objects(blobs), start=1):
            if index % 25 == 0:
                report(f"Fitting craters (pass {passed + 1} of 2)",
                       base + 0.45 + 0.05 * index / max(count, 1))
            blob = blobs[window] == index
            r_idx, c_idx = np.nonzero(blob)
            r_idx, c_idx = r_idx + window[0].start, c_idx + window[1].start
            centre = (r_idx.mean(), c_idx.mean())
            if covered[int(round(centre[0])), int(round(centre[1]))]:
                continue                       # Already part of a kept crater
            enclosing = np.hypot(r_idx - centre[0], c_idx - centre[1]).max() + 0.5
            if blob.sum() / (np.pi * enclosing ** 2) < MIN_ROUNDNESS:
                continue
            seed_radius = max(np.sqrt(blob.sum() / np.pi), 1.0)
            circle = _rim_circle(dem, centre, seed_radius)
            if circle is None:
                # No clear crest all round (e.g. cut by the map edge): keep the
                # seed itself, judged by its own depth.
                radius, depth = seed_radius, float(depression[window][blob].max())
            else:
                centre, radius, crest_z = circle
            r0, r1 = int(max(0, centre[0] - radius - 1)), int(min(dem.shape[0], centre[0] + radius + 2))
            c0, c1 = int(max(0, centre[1] - radius - 1)), int(min(dem.shape[1], centre[1] + radius + 2))
            if r0 >= r1 or c0 >= c1:
                continue
            yy, xx = np.mgrid[r0:r1, c0:c1]
            disc = np.hypot(yy - centre[0], xx - centre[1]) <= radius
            if not disc.any():
                continue
            if circle is not None:
                depth = crest_z - float(dem[r0:r1, c0:c1][disc].min())
            if depth < MIN_DEPTH_RATIO * 2 * radius * resolution:
                continue
            if covered[r0:r1, c0:c1][disc].mean() > MAX_OVERLAP:
                continue
            covered[r0:r1, c0:c1] |= disc
            craters.append((float(centre[0]), float(centre[1]), float(radius)))
    report("Craters found", 1.0)
    return craters


def render_labels(shape, craters, crater_id=CRATER_ID, rim_id=RIM_ID,
                  rim_fraction=RIM_FRACTION, supersample=1):
    """
    uint8 map of (rows * supersample, cols * supersample): crater_id inside each
    circle, rim_id in the ring out to (1 + rim_fraction) x radius, 0 elsewhere.
    Map cell (i, j) covers DEM coordinates ((i + 0.5) / s - 0.5, (j + 0.5) / s - 0.5).
    """
    k = int(supersample)
    rows, cols = shape[0] * k, shape[1] * k
    labels = np.zeros((rows, cols), dtype=np.uint8)
    rims = np.zeros((rows, cols), dtype=bool)
    grow = 1 + max(rim_fraction, 0.0) if rim_id else 1.0
    for row, col, radius in craters:
        cr, cc, outer = (row + 0.5) * k - 0.5, (col + 0.5) * k - 0.5, radius * grow * k
        r0, r1 = int(max(0, cr - outer - 1)), int(min(rows, cr + outer + 2))
        c0, c1 = int(max(0, cc - outer - 1)), int(min(cols, cc + outer + 2))
        if r0 >= r1 or c0 >= c1:
            continue
        yy, xx = np.ogrid[r0:r1, c0:c1]
        distance = np.hypot(yy - cr, xx - cc)
        labels[r0:r1, c0:c1][distance <= radius * k] = crater_id
        if grow > 1.0:
            rims[r0:r1, c0:c1] |= distance <= outer
    labels[rims & (labels == 0)] = rim_id          # Craters win over neighbouring rims
    return labels


def crater_labels(dem, resolution, min_depth_m, crater_id=CRATER_ID, rim_id=RIM_ID,
                  rim_fraction=RIM_FRACTION, supersample=1):
    """
    uint8 map: crater_id inside rim crests, rim_id on the rims, 0 elsewhere, at
    `supersample` cells per DEM cell. Rims are disabled with rim_fraction <= 0
    or rim_id == 0. Returns (labels, crater_count).
    """
    dem = np.asarray(dem, dtype=float)
    craters = find_craters(dem, resolution, min_depth_m)
    labels = render_labels(dem.shape, craters, crater_id, rim_id, rim_fraction, supersample)
    missing = ~np.isfinite(dem)
    if missing.any():
        labels[np.kron(missing, np.ones((supersample, supersample), dtype=bool))] = 0
    return labels, len(craters)


def export_crater_mask(labels, output_dir, base_name, resolution, min_depth_m,
                       crater_id=CRATER_ID, rim_id=RIM_ID, rim_fraction=RIM_FRACTION,
                       supersample=1):
    """
    Writes <group>_craterMask.png (8-bit, values are mask ids, row 0 = DEM row 0,
    `supersample` cells per DEM cell) and <group>_craterMask.json beside the
    map's other outputs. Returns {'crater_mask_png': path, 'crater_mask_json': path}.
    """
    group = base_name.replace('_elevationMap', '')
    target_dir = os.path.join(output_dir, group)
    os.makedirs(target_dir, exist_ok=True)
    png = os.path.join(target_dir, f"{group}_craterMask.png")
    meta = os.path.join(target_dir, f"{group}_craterMask.json")
    Image.fromarray(labels.astype(np.uint8), mode="L").save(png)
    with open(meta, "w") as handle:
        json.dump({
            "width": int(labels.shape[1]), "height": int(labels.shape[0]),
            "supersample": int(supersample),
            "resolution_m": float(resolution), "min_depth_m": float(min_depth_m),
            "crater_id": int(crater_id), "rim_id": int(rim_id),
            "rim_fraction": float(rim_fraction),
            "crater_cover": float((labels == crater_id).mean()),
            "rim_cover": float((labels == rim_id).mean()),
        }, handle, indent=2)
    return {"crater_mask_png": png, "crater_mask_json": meta}
