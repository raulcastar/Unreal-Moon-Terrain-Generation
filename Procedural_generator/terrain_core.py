"""
Procedural Lunar Terrain Core
-----------------------------
Importable generation engine shared by the CLI (`procedural_landscape_gen.py`)
and the graphical front-end (`Terrain_Studio/terrain_studio.py`).

The pipeline is unchanged from the original script: Fractal-ish Gaussian macro
terrain, stochastic collision-aware crater placement, and sub-surface dithering
to prevent quantization banding in the 3D engine. The only structural change is
that every parameter is now passed explicitly instead of living in module-level
globals, so the same code path can be driven from a UI.
"""

import os

import numpy as np
import pandas as pd
import yaml
from scipy.ndimage import gaussian_filter, zoom

# Longest edge of the grid the low-frequency macro terrain is built on. The
# macro field is smoothed with a blur radius that scales with the map, so a
# direct build costs O(N^2 * sigma) = O(N^3). Because the field is intentionally
# low-frequency (only ~10 features span the whole map), it is instead built and
# eroded on a grid capped at this size and bilinearly upsampled to the target.
# Maps at or below the cap are built natively and are unaffected.
MACRO_GRID_CAP = 1024


class TerrainCancelled(Exception):
    """Raised out of generate_terrain/export_outputs when cancel_check() is true."""


# =========================================================================
# PARAMETER SCHEMA
# =========================================================================

# Canonical defaults. Any config file is merged on top of this dictionary, so
# older YAML files missing newer keys keep working.
DEFAULTS = {
    # --- Spatial Dimensions & Base Level ---
    'ROWS': 1000,
    'COLS': 1000,
    'BASE_ELEVATION': 40.0,
    'RANDOM_SEED': None,            # Integer enforces deterministic generation

    # --- Spatial scale (consumed by the Unreal loader, not by the generator) ---
    'RESOLUTION_M_PER_PX': 1.0,     # Meters per pixel of the generated DEM

    # --- Macro-Terrain (Low-Frequency Noise) ---
    'MACRO_ELEVATION_MIN': 0.5,
    'MACRO_ELEVATION_MAX': 1.0,

    # --- Micro-Terrain (High-Frequency Noise / Dithering) ---
    'MICRO_ROUGHNESS': 0.001,

    # --- Crater Morphology ---
    'CRATER_DENSITY': 0.0001,
    'CRATER_MIN_RADIUS_PCT': 0.01,
    'CRATER_MAX_RADIUS_PCT': 0.12,
    'CRATER_DEPTH_RATIO': 0.01,
    'CRATER_RIM_HEIGHT_RATIO': 0.2,

    # --- Post-Processing ---
    'EROSION_BLUR_DIVISOR': 50.0,

    # --- I/O Serialization ---
    'EXPORT_CSV': True,
    'EXPORT_NPY': True,
}

# Keys that must stay integral when round-tripped through the UI / YAML.
_INT_KEYS = ('ROWS', 'COLS')


def default_params():
    """Returns a fresh copy of the canonical parameter set."""
    return dict(DEFAULTS)


def coerce_params(raw):
    """
    Merges a raw config dictionary over the defaults and casts every value to
    its expected runtime type. Unknown keys are preserved untouched so custom
    annotations in a YAML file survive a load/save round-trip.
    """
    params = dict(DEFAULTS)
    params.update(raw or {})

    for key in _INT_KEYS:
        params[key] = int(params[key])

    for key in ('BASE_ELEVATION', 'RESOLUTION_M_PER_PX', 'MACRO_ELEVATION_MIN',
                'MACRO_ELEVATION_MAX', 'MICRO_ROUGHNESS', 'CRATER_DENSITY',
                'CRATER_MIN_RADIUS_PCT', 'CRATER_MAX_RADIUS_PCT',
                'CRATER_DEPTH_RATIO', 'CRATER_RIM_HEIGHT_RATIO',
                'EROSION_BLUR_DIVISOR'):
        params[key] = float(params[key])

    for key in ('EXPORT_CSV', 'EXPORT_NPY'):
        params[key] = bool(params[key])

    seed = params.get('RANDOM_SEED', None)
    params['RANDOM_SEED'] = None if seed in (None, '', 'null') else int(seed)

    return params


def load_config(config_path):
    """Reads a YAML configuration file and returns a fully coerced parameter dict."""
    with open(config_path, 'r') as handle:
        raw = yaml.safe_load(handle)
    return coerce_params(raw)


def save_config(config_path, params):
    """Serializes a parameter dict back to YAML, preserving key order."""
    directory = os.path.dirname(config_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    ordered = {key: params[key] for key in DEFAULTS if key in params}
    ordered.update({k: v for k, v in params.items() if k not in DEFAULTS})

    with open(config_path, 'w') as handle:
        yaml.safe_dump(ordered, handle, sort_keys=False, default_flow_style=False)


def write_default_config(config_path):
    """Emits the annotated default configuration template used by the CLI."""
    directory = os.path.dirname(config_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    template = """# ==========================================
# LUNAR TERRAIN GENERATOR CONFIGURATION
# ==========================================

# --- Spatial Dimensions & Base Level ---
ROWS: 1000
COLS: 1000
BASE_ELEVATION: 40.0
RANDOM_SEED: null           # Set to an integer to enforce deterministic generation

# --- Spatial Scale (used by the Unreal loader) ---
RESOLUTION_M_PER_PX: 1.0    # Meters covered by a single DEM pixel

# --- Macro-Terrain (Low-Frequency Noise) ---
MACRO_ELEVATION_MIN: 0.5    # Minimum elevation variance (meters)
MACRO_ELEVATION_MAX: 1.0    # Maximum elevation variance (meters)

# --- Micro-Terrain (High-Frequency Noise / Dithering) ---
MICRO_ROUGHNESS: 0.001      # Amplitude of Gaussian noise applied to prevent banding

# --- Crater Morphology ---
CRATER_DENSITY: 0.0001      # Area multiplier determining total crater count
CRATER_MIN_RADIUS_PCT: 0.01 # Minimum radius bounds (percentage of max map dimension)
CRATER_MAX_RADIUS_PCT: 0.12 # Maximum radius bounds (percentage of max map dimension)
CRATER_DEPTH_RATIO: 0.01    # Depth calculation ratio relative to crater radius
CRATER_RIM_HEIGHT_RATIO: 0.2 # Ejecta rim height relative to internal crater depth

# --- Post-Processing ---
EROSION_BLUR_DIVISOR: 50.0  # Dynamic blur scaling. Lower values = stronger erosion smoothing.

# --- I/O Serialization ---
EXPORT_CSV: true
EXPORT_NPY: true
"""
    with open(config_path, 'w') as handle:
        handle.write(template.strip())


# =========================================================================
# CORE GENERATION LOGIC
# =========================================================================

def add_crater(matrix, center_row, center_col, radius, depth, rim_height_ratio):
    """
    Applies a procedural crater to the target elevation matrix using a
    parabolic impact model and an exponentially decaying ejecta rim.

    Performance Optimization:
    Calculates localized changes via a bounding box subset rather than
    evaluating the full n-dimensional array, reducing time complexity.

    Args:
        matrix (np.ndarray): The target elevation map.
        center_row (int): Y-axis origin of the impact.
        center_col (int): X-axis origin of the impact.
        radius (float): The base radius of the crater bowl.
        depth (float): Maximum vertical displacement at the center of the bowl.
        rim_height_ratio (float): Ejecta rim height relative to the bowl depth.
    """
    rows, cols = matrix.shape

    # Define bounding box localized to the impact footprint (radius * 1.5 allows for rim)
    max_dist = int(np.ceil(radius * 1.5))
    r_min = max(0, center_row - max_dist)
    r_max = min(rows, center_row + max_dist + 1)
    c_min = max(0, center_col - max_dist)
    c_max = min(cols, center_col + max_dist + 1)

    # Abort if the localized bounding box falls completely outside the map bounds
    if r_min >= r_max or c_min >= c_max:
        return

    # Generate a localized coordinate grid for rapid distance calculations
    r_grid, c_grid = np.ogrid[r_min:r_max, c_min:c_max]
    dist = np.sqrt((r_grid - center_row) ** 2 + (c_grid - center_col) ** 2)

    # Segregate spatial zones: Internal impact bowl vs. external ejecta rim
    bowl_mask = dist <= radius
    rim_mask = (dist > radius) & (dist <= radius * 1.5)

    local_matrix = matrix[r_min:r_max, c_min:c_max]

    # Model the internal parabolic depression
    local_matrix[bowl_mask] -= depth * (1 - (dist[bowl_mask] / radius) ** 2)

    # Model the external rim using quadratic decay for a smooth morphological blend
    rim_dist_normalized = (dist[rim_mask] - radius) / (radius * 0.5)
    rim_height = (depth * rim_height_ratio) * (1 - rim_dist_normalized) ** 2
    local_matrix[rim_mask] += rim_height


def generate_terrain(params, rows=None, cols=None, crater_count=None,
                     seed=None, progress=None, cancel_check=None):
    """
    Executes the procedural generation pipeline:
      1. Base initialization
      2. Macro-terrain noise injection
      3. Structural Gaussian smoothing
      4. Collision-aware impact processing (Craters)
      5. Sub-surface dithering (Anti-banding)

    Args:
        params (dict): Coerced parameter dictionary (see `coerce_params`).
        rows (int|None): Row override, used to render reduced-resolution previews.
        cols (int|None): Column override, used to render reduced-resolution previews.
        crater_count (int|None): Explicit impact count. When a preview is rendered
            at a reduced resolution the caller passes the full-resolution count so
            the crater population stays morphologically representative.
        seed (int|None): Seed override. Falls back to `params['RANDOM_SEED']`.
        progress (callable|None): Optional `fn(message, fraction)` status hook.
        cancel_check (callable|None): Optional `fn() -> bool`. Polled at phase
            boundaries and during the crater loop; a truthy result aborts the run
            by raising `TerrainCancelled`.

    Returns:
        dict: {'elevation', 'rows', 'cols', 'seed', 'crater_count', 'craters'}
    """
    def report(message, fraction):
        if cancel_check and cancel_check():
            raise TerrainCancelled()
        if progress:
            progress(message, fraction)

    rows = int(rows if rows is not None else params['ROWS'])
    cols = int(cols if cols is not None else params['COLS'])

    base_elevation = float(params['BASE_ELEVATION'])
    macro_min = float(params['MACRO_ELEVATION_MIN'])
    macro_max = float(params['MACRO_ELEVATION_MAX'])
    micro_roughness = float(params['MICRO_ROUGHNESS'])
    crater_density = float(params['CRATER_DENSITY'])
    min_radius_pct = float(params['CRATER_MIN_RADIUS_PCT'])
    max_radius_pct = float(params['CRATER_MAX_RADIUS_PCT'])
    depth_ratio = float(params['CRATER_DEPTH_RATIO'])
    rim_height_ratio = float(params['CRATER_RIM_HEIGHT_RATIO'])
    erosion_divisor = float(params['EROSION_BLUR_DIVISOR'])

    # Establish deterministic vs. stochastic behavior based on config
    if seed is None:
        seed = params.get('RANDOM_SEED', None)
    np.random.seed(seed)

    report(f"Initializing {rows}x{cols} elevation matrix", 0.05)
    max_dimension = max(rows, cols)

    # --- Phase 1: Macro-Terrain Construction ---
    # Build the low-frequency field on a grid whose longest edge is capped, then
    # upsample. The macro blur and the erosion blur are both linear and both act
    # on purely low-frequency content (craters are added afterwards), so applying
    # them on the reduced grid and interpolating is equivalent to the original
    # full-resolution build for maps at or below the cap, and a close, bounded-cost
    # approximation above it. This removes the O(N^3) blur that made large maps
    # take tens of minutes.
    report("Synthesizing macro-terrain", 0.15)
    grid_scale = min(1.0, MACRO_GRID_CAP / max_dimension)
    grid_rows = max(2, int(round(rows * grid_scale)))
    grid_cols = max(2, int(round(cols * grid_scale)))
    grid_dimension = max(grid_rows, grid_cols)

    macro_noise = np.random.normal(loc=0.0, scale=1.0, size=(grid_rows, grid_cols))
    macro_noise = gaussian_filter(macro_noise, sigma=grid_dimension / 10.0)

    # Normalize noise matrix and map to configured elevation amplitudes
    macro_noise -= np.min(macro_noise)
    peak = np.max(macro_noise)
    if peak != 0:
        macro_noise /= peak
    target_differential = np.random.uniform(macro_min, macro_max)
    macro_noise = (macro_noise * target_differential) - (target_differential / 2.0)

    # Add the base datum and apply heavy structural smoothing, both on the grid.
    # When the grid equals the target (map at or below the cap) this is the exact
    # same sequence of operations as a direct full-resolution build, so those
    # maps are bit-identical; above the cap the eroded field is upsampled.
    field = np.full((grid_rows, grid_cols), base_elevation)
    field += macro_noise

    report("Applying erosion pass", 0.35)
    dynamic_sigma = max(1.0, grid_dimension / erosion_divisor)
    field = gaussian_filter(field, sigma=dynamic_sigma)

    if (grid_rows, grid_cols) != (rows, cols):
        field = zoom(field, (rows / grid_rows, cols / grid_cols),
                     order=1, mode='nearest')
        # zoom's output shape can be off by a pixel from floating-point rounding;
        # crop or edge-pad back to the exact target so later full-size arrays add.
        field = field[:rows, :cols]
        pad_r = rows - field.shape[0]
        pad_c = cols - field.shape[1]
        if pad_r or pad_c:
            field = np.pad(field, ((0, pad_r), (0, pad_c)), mode='edge')

    elevation_matrix = field

    # --- Phase 2: Impact Generation & Collision Handling ---
    if crater_count is None:
        crater_count = int(rows * cols * crater_density)
    crater_count = max(0, int(crater_count))

    # Registry of accepted impacts as (row, col, radius) for vectorized collision tests
    registry = np.empty((crater_count, 3), dtype=float)
    placed = 0
    attempts = 0
    max_attempts = crater_count * 10  # Hard threshold to prevent infinite loops

    report(f"Placing {crater_count} impacts", 0.45)

    min_rad = max_dimension * min_radius_pct
    max_rad = max_dimension * max_radius_pct

    while placed < crater_count and attempts < max_attempts:
        attempts += 1

        # Poll for cancellation even while only rejecting candidates, since the
        # progress report below fires only on successful placement.
        if cancel_check and attempts % 512 == 0 and cancel_check():
            raise TerrainCancelled()

        # Calculate random spatial coordinates and physical morphology limits
        r_center = np.random.randint(0, rows)
        c_center = np.random.randint(0, cols)

        # Exponential bias applied via exponentiation to favor smaller impacts
        rad = min_rad + (max_rad - min_rad) * (np.random.random() ** 3)

        # Execute Euclidean distance comparisons against registered impacts.
        # Vectorized so dense fields (thousands of craters) stay tractable.
        if placed:
            existing = registry[:placed]
            distances = np.hypot(existing[:, 0] - r_center, existing[:, 1] - c_center)
            # Account for the extended ejecta rim footprint on both craters
            if np.any(distances < (rad * 1.5) + (existing[:, 2] * 1.5)):
                continue

        # Register and process valid impact events
        add_crater(elevation_matrix, r_center, c_center, rad,
                   rad * depth_ratio, rim_height_ratio)
        registry[placed] = (r_center, c_center, rad)
        placed += 1

        if crater_count >= 200 and placed % 200 == 0:
            report(f"Placing impacts ({placed}/{crater_count})",
                   0.45 + 0.35 * (placed / crater_count))

    # --- Phase 3: High-Frequency Noise & Post-Processing ---
    # Inject high-frequency Gaussian noise to act as 3D spatial dithering,
    # preventing integer-rounding quantization artifacts (terracing/banding).
    report("Applying micro-roughness dithering", 0.85)
    elevation_matrix += np.random.normal(loc=0.0, scale=micro_roughness, size=(rows, cols))

    # Apply sub-pixel anti-aliasing to soften harsh noise artifacts
    elevation_matrix = gaussian_filter(elevation_matrix, sigma=0.8)

    report("Generation complete", 1.0)

    return {
        'elevation': elevation_matrix,
        'rows': rows,
        'cols': cols,
        'seed': seed,
        'crater_count': placed,
        'craters': registry[:placed],
    }


def full_crater_count(params):
    """
    Returns the impact count of the full-resolution map.

    A reduced-resolution preview must place this same count rather than its own
    area product: crater radii are a percentage of the largest map dimension and
    the erosion sigma divides that same dimension, so both shrink with the
    preview. Reusing the full count keeps the relative surface coverage — and
    therefore the visual morphology — identical to the final export.
    """
    return int(params['ROWS'] * params['COLS'] * params['CRATER_DENSITY'])


# =========================================================================
# SERIALIZATION
# =========================================================================

def export_outputs(elevation_matrix, output_dir, base_name='generated_elevationMap_moon',
                   export_csv=True, export_npy=True, progress=None, cancel_check=None):
    """
    Serializes the elevation matrix to the formats consumed downstream.

    Returns:
        dict: Mapping of format key -> absolute written file path.
    """
    def report(message, fraction):
        if cancel_check and cancel_check():
            raise TerrainCancelled()
        if progress:
            progress(message, fraction)

    os.makedirs(output_dir, exist_ok=True)
    written = {}

    if export_csv:
        report("Serializing CSV datasets", 0.3)
        elevation_csv = os.path.join(output_dir, f"{base_name}.csv")
        pd.DataFrame(elevation_matrix).to_csv(elevation_csv, index=False, header=False, sep=' ')
        written['elevation_csv'] = elevation_csv

        # Generate edge-masked soil layer for 3D engine material blending
        soil_matrix = np.zeros(elevation_matrix.shape, dtype=int)
        soil_matrix[1:-1, 1:-1] = 1
        soil_csv = os.path.join(output_dir, f"{base_name.replace('_elevationMap', '')}_soilMap_edges_0.csv")
        pd.DataFrame(soil_matrix).to_csv(soil_csv, index=False, header=False, sep=' ')
        written['soil_csv'] = soil_csv

    if export_npy:
        report("Serializing NumPy binary", 0.8)
        elevation_npy = os.path.join(output_dir, f"{base_name}.npy")
        np.save(elevation_npy, elevation_matrix)
        written['elevation_npy'] = elevation_npy

    report("Export complete", 1.0)
    return written
