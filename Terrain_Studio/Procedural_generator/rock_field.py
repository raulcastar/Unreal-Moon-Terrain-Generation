"""
Rock-field and rover-path planning for a lunar DEM.

Given an elevation map, this module decides where to scatter rocks as obstacles
while keeping a network of drivable corridors between the craters:

  1. Cells whose slope exceeds the rover limit are marked impassable, as are
     craters (closed depressions), which are also off-limits for rocks.
  2. The roads follow the Voronoi diagram of the craters: each crater is a site
     and their shared edges are the corridors, bridged into one connected net.
  3. Those corridors, widened by the rover clearance, are protected: no rock is
     ever placed within a rock-radius of them. The corridors therefore stay
     open no matter how many rocks are scattered elsewhere.

All heavy work runs on a grid capped at PLAN_GRID_CAP: routing and placement do
not need the full DEM resolution, and the results scale back to world metres.

The module is UI-free so it can be driven from the studio or a script. It shares
the progress/cancel convention and the TerrainCancelled exception with
terrain_core.
"""

import heapq
import os

import numpy as np
import pandas as pd
from scipy import ndimage
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import Voronoi, QhullError

from terrain_core import TerrainCancelled

# Longest edge of the grid used for traversability, routing, and placement.
# Path quality and rock positions do not need full DEM resolution; capping keeps
# the graph build and the rejection sampling fast on large maps.
PLAN_GRID_CAP = 512
SQRT2 = float(np.sqrt(2.0))

# Cube bias pushes rock radii towards the minimum (as with craters); the mean of
# u**3 for u uniform on [0, 1] is 0.25, used to estimate the average rock area.
_CUBE_BIAS_MEAN = 0.25


DEFAULTS = {
    # --- Rover corridor ---
    # Full width of the cleared rover path; no rock is placed within half of this
    # of a road (the old separate path margin is folded into this width).
    'PATH_WIDTH_M': 4.0,

    # --- Crater hazards ---
    'MIN_CRATER_DEPTH_M': 0.5,     # Closed depressions this deep are impassable craters.

    # --- Rock population ---
    'ROCK_COVERAGE': 0.03,         # Target fraction of drivable ground under rocks.
    # Rock size as a scale (metre radius of a 1 m base mesh), cubically biased
    # towards the minimum.
    'ROCK_MIN_SCALE': 0.3,
    'ROCK_MAX_SCALE': 2.5,
    'ROCK_HEIGHT_RATIO': 0.7,      # Rock height as a fraction of its radius.
}

_INT_KEYS = ()


def default_params():
    """Returns a fresh copy of the canonical rock-planning parameters."""
    return dict(DEFAULTS)


def coerce_params(raw):
    """Merges a raw dict over the defaults and casts every value to its type."""
    params = dict(DEFAULTS)
    params.update(raw or {})
    for key in _INT_KEYS:
        params[key] = max(0, int(round(float(params[key]))))
    for key in ('PATH_WIDTH_M', 'MIN_CRATER_DEPTH_M', 'ROCK_COVERAGE',
                'ROCK_MIN_SCALE', 'ROCK_MAX_SCALE', 'ROCK_HEIGHT_RATIO'):
        params[key] = float(params[key])
    if params['ROCK_MAX_SCALE'] < params['ROCK_MIN_SCALE']:
        params['ROCK_MAX_SCALE'] = params['ROCK_MIN_SCALE']
    return params


# =========================================================================
# GRID PREPARATION
# =========================================================================

def _plan_grid(elevation, resolution, cap):
    """
    Downsamples the DEM to the planning grid.

    Returns (plan_elevation, res_row, res_col, scale_row, scale_col) where the
    two resolutions are metres per planning pixel along each axis and the scales
    map a planning index back to a full-resolution index.
    """
    rows, cols = elevation.shape
    scale = min(1.0, cap / max(rows, cols))
    plan_rows = max(16, int(round(rows * scale)))
    plan_cols = max(16, int(round(cols * scale)))

    if (plan_rows, plan_cols) == (rows, cols):
        plan = elevation.astype(float, copy=True)
    else:
        plan = ndimage.zoom(elevation.astype(float),
                            (plan_rows / rows, plan_cols / cols),
                            order=1, mode='nearest')
        plan = plan[:plan_rows, :plan_cols]

    res_row = resolution * rows / plan_rows
    res_col = resolution * cols / plan_cols
    return plan, res_row, res_col, rows / plan_rows, cols / plan_cols


def _fill_depressions(elevation):
    """
    Priority-flood sink fill (Barnes 2014): returns a surface equal to `elevation`
    except that every closed basin is raised to the height of its lowest outlet.
    `filled - elevation` is then the depth of standing water each cell would hold,
    which isolates crater bowls from ordinary valleys that drain off the edge.
    """
    rows, cols = elevation.shape
    filled = np.empty_like(elevation, dtype=float)
    closed = np.zeros((rows, cols), dtype=bool)

    # Seed the queue with the map border (the only guaranteed outlets).
    heap = []
    for c in range(cols):
        for r in (0, rows - 1):
            heap.append((float(elevation[r, c]), r, c))
            closed[r, c] = True
            filled[r, c] = elevation[r, c]
    for r in range(1, rows - 1):
        for c in (0, cols - 1):
            heap.append((float(elevation[r, c]), r, c))
            closed[r, c] = True
            filled[r, c] = elevation[r, c]
    heapq.heapify(heap)

    neighbours = ((-1, 0), (1, 0), (0, -1), (0, 1))
    while heap:
        level, r, c = heapq.heappop(heap)
        for dr, dc in neighbours:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not closed[nr, nc]:
                closed[nr, nc] = True
                value = elevation[nr, nc]
                raised = value if value > level else level
                filled[nr, nc] = raised
                heapq.heappush(heap, (raised, nr, nc))
    return filled


def detect_craters(elevation, min_depth):
    """
    Returns (crater_mask, depression) where crater_mask marks closed depressions
    at least `min_depth` metres deep. Single-pixel speckle (from micro-roughness)
    is removed with a morphological opening.

    The DEM is reflection-padded first so the map border acts as a no-flow wall
    rather than a drainage outlet: a crater straddling the edge is mirrored into a
    full enclosed basin and is detected, instead of appearing to drain off the map
    and being missed.
    """
    rows, cols = elevation.shape
    margin = max(16, int(round(0.1 * max(rows, cols))))
    padded = np.pad(elevation, margin, mode='reflect')
    depression = (_fill_depressions(padded) - padded)[
        margin:margin + rows, margin:margin + cols]
    mask = depression >= max(float(min_depth), 1e-6)
    mask = ndimage.binary_opening(mask, structure=np.ones((3, 3)))
    return mask, depression


def _largest_component(passable):
    """Returns a mask of the largest 8-connected drivable region (or all-False)."""
    labels, count = ndimage.label(passable, structure=np.ones((3, 3)))
    if count == 0:
        return np.zeros_like(passable, dtype=bool), labels, 0
    sizes = ndimage.sum(np.ones_like(labels), labels, index=np.arange(1, count + 1))
    largest = int(np.argmax(sizes)) + 1
    return labels == largest, labels, largest


# =========================================================================
# CORRIDOR ROUTING (generalized Voronoi diagram of the craters)
# =========================================================================

def _cost_graph(cost, passable):
    """Builds an 8-connected sparse cost graph over the passable cells."""
    rows, cols = cost.shape
    index = np.arange(rows * cols).reshape(rows, cols)
    offsets = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
               (-1, -1, SQRT2), (-1, 1, SQRT2), (1, -1, SQRT2), (1, 1, SQRT2)]

    src, dst, weight = [], [], []
    for dr, dc, dist in offsets:
        rs = slice(max(0, -dr), rows - max(0, dr))
        cs = slice(max(0, -dc), cols - max(0, dc))
        rd = slice(max(0, dr), rows - max(0, -dr))
        cd = slice(max(0, dc), cols - max(0, -dc))

        both = passable[rs, cs] & passable[rd, cd]
        edge_w = dist * 0.5 * (cost[rs, cs] + cost[rd, cd])
        src.append(index[rs, cs][both])
        dst.append(index[rd, cd][both])
        weight.append(edge_w[both])

    n = rows * cols
    return csr_matrix((np.concatenate(weight),
                       (np.concatenate(src), np.concatenate(dst))), shape=(n, n))


def _prim_edges(distance):
    """Minimum spanning tree over a dense k x k cost matrix; returns (i, j) edges."""
    k = distance.shape[0]
    if k < 2:
        return []
    in_tree = np.zeros(k, dtype=bool)
    in_tree[0] = True
    best = distance[0].copy()
    parent = np.zeros(k, dtype=int)
    edges = []
    for _ in range(k - 1):
        best_masked = np.where(in_tree, np.inf, best)
        j = int(np.argmin(best_masked))
        if not np.isfinite(best_masked[j]):
            break  # remaining waypoints are unreachable
        in_tree[j] = True
        edges.append((parent[j], j))
        closer = distance[j] < best
        best = np.where(closer, distance[j], best)
        parent = np.where(closer, j, parent)
    return edges


def _reconstruct(predecessor_row, target):
    """Walks a csgraph predecessor row back to its source; returns cell indices."""
    path, node = [], int(target)
    while node >= 0:
        path.append(node)
        node = int(predecessor_row[node])
    return path


# --- Voronoi routing: craters are the sites, their shared edges are the roads --

def _crater_sites(crater_mask):
    """Centroid (row, col) of every crater blob, used as Voronoi sites."""
    labels, count = ndimage.label(crater_mask, structure=np.ones((3, 3)))
    if count == 0:
        return np.empty((0, 2))
    centres = ndimage.center_of_mass(
        np.ones_like(labels, dtype=float), labels, np.arange(1, count + 1))
    return np.array(centres, dtype=float)


def _sample_segment(p0, p1):
    """Rasterizes the segment p0->p1 to integer (rows, cols) at ~1 px spacing."""
    length = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
    steps = max(2, int(np.ceil(length)) + 1)
    t = np.linspace(0.0, 1.0, steps)
    rows = np.round(p0[0] + t * (p1[0] - p0[0])).astype(int)
    cols = np.round(p0[1] + t * (p1[1] - p0[1])).astype(int)
    return rows, cols


def _voronoi_corridors(region, crater_mask, progress):
    """
    Builds the corridor network from the Voronoi diagram of the crater centroids:
    each crater is a site, and the drivable parts of the Voronoi ridges (the loci
    equidistant between neighbouring craters) become the roads. Returns
    (corridor, paths, sites), or None if there are fewer than two craters or no
    drivable edges (the caller then produces no corridors).
    """
    rows, cols = region.shape
    sites = _crater_sites(crater_mask)
    if len(sites) < 2:
        return None

    # Distant corner sites bound the diagram so border ridges are finite.
    pad = float(max(rows, cols))
    corners = np.array([[-pad, -pad], [-pad, cols + pad],
                        [rows + pad, -pad], [rows + pad, cols + pad]])
    try:
        vor = Voronoi(np.vstack([sites, corners]))
    except QhullError:
        return None

    corridor = np.zeros((rows, cols), dtype=bool)
    paths = []
    for a, b in vor.ridge_vertices:
        if a < 0 or b < 0:
            continue  # infinite ridge; the corner sites keep the useful ones finite
        seg_rows, seg_cols = _sample_segment(vor.vertices[a], vor.vertices[b])
        run_r, run_c = [], []
        for r, c in zip(seg_rows, seg_cols):
            if 0 <= r < rows and 0 <= c < cols and region[r, c]:
                corridor[r, c] = True
                run_r.append(r)
                run_c.append(c)
            elif len(run_r) >= 2:      # ridge left the drivable region; close the run
                paths.append((np.array(run_r), np.array(run_c)))
                run_r, run_c = [], []
            else:
                run_r, run_c = [], []
        if len(run_r) >= 2:
            paths.append((np.array(run_r), np.array(run_c)))

    if not corridor.any():
        return None
    if progress:
        progress("Traced Voronoi corridors", 0.4)
    return corridor, paths, sites


def _connect_components(cost, region, corridor, progress):
    """
    Bridges the disjoint pieces of `corridor` into one connected network with
    least-cost links through the drivable region, so a Voronoi road broken by
    steep ground is rejoined. Returns (corridor, bridge_paths).
    """
    labels, count = ndimage.label(corridor, structure=np.ones((3, 3)))
    if count <= 1:
        return corridor, []

    cols = region.shape[1]
    reps = []
    for component in range(1, count + 1):
        rs, cs = np.nonzero(labels == component)
        reps.append((int(rs[0]), int(cs[0])))

    graph = _cost_graph(cost, region)
    sources = [r * cols + c for r, c in reps]
    distances, predecessors = dijkstra(graph, directed=False, indices=sources,
                                       return_predecessors=True)
    pair_cost = distances[:, sources]
    pair_cost = np.minimum(pair_cost, pair_cost.T)

    bridges = []
    for i, j in _prim_edges(pair_cost):
        cells = _reconstruct(predecessors[i], sources[j])
        if len(cells) < 2:
            continue
        rr, cc = np.divmod(np.asarray(cells), cols)
        corridor[rr, cc] = True
        bridges.append((rr, cc))
    if progress:
        progress("Bridged Voronoi corridors", 0.5)
    return corridor, bridges


# =========================================================================
# ROCK PLACEMENT
# =========================================================================

# Upper bound on rocks per plan. Small rocks on large maps can otherwise
# require millions of instances (one Unreal actor each).
MAX_ROCKS = 100_000

# Rocks with a radius below half a planning cell fit within a single cell.
_CELL_FIT = 0.5
_SQRT2 = float(np.sqrt(2.0))


def estimate_rock_count(params, drivable_area_m2):
    """
    Returns the target rock count for `drivable_area_m2` of drivable ground
    (coverage x area / mean footprint), before the MAX_ROCKS cap.
    """
    min_r = params['ROCK_MIN_SCALE']
    max_r = max(min_r, params['ROCK_MAX_SCALE'])
    mean_r = min_r + (max_r - min_r) * _CUBE_BIAS_MEAN
    return max(0, int(params['ROCK_COVERAGE'] * drivable_area_m2
                      / max(np.pi * mean_r ** 2, 1e-12)))


def _stamp_disk(mask, row, col, radius):
    """Marks a filled disk of the given radius into a boolean mask, in place."""
    rows, cols = mask.shape
    reach = int(np.ceil(radius)) + 1
    r_mid, c_mid = int(round(row)), int(round(col))
    r0, r1 = max(0, r_mid - reach), min(rows, r_mid + reach + 1)
    c0, c1 = max(0, c_mid - reach), min(cols, c_mid + reach + 1)
    if r0 >= r1 or c0 >= c1:
        return
    rr, cc = np.ogrid[r0:r1, c0:c1]
    mask[r0:r1, c0:c1] |= (rr - row) ** 2 + (cc - col) ** 2 <= radius ** 2


def _place_rocks(region, corridor, crater_mask, params, res_row, res_col, seed,
                 progress, cancel_check):
    """
    Scatters rocks over the drivable region while keeping clear of the corridors
    and craters.

    Returns (registry, occupancy, requested): an (N, 3) array of
    (row, col, radius_px), the rock footprint mask, and the target count before
    the MAX_ROCKS cap.

    Sub-cell rocks are jittered within their cell to avoid grid alignment. At
    most one is placed per cell, which precludes overlaps between them without
    pairwise checks.
    """
    rows, cols = region.shape
    mean_res = 0.5 * (res_row + res_col)
    half_width_px = 0.5 * params['PATH_WIDTH_M'] / mean_res
    min_r_px = params['ROCK_MIN_SCALE'] / mean_res
    max_r_px = max(min_r_px, params['ROCK_MAX_SCALE'] / mean_res)

    occupancy = np.zeros((rows, cols), dtype=bool)

    # Distance (in px) from every cell to the nearest corridor cell. A rock is
    # rejected unless its whole footprint plus half the path width clears the
    # corridor, which is what keeps the drivable lane open.
    if corridor.any():
        dist_to_corridor = ndimage.distance_transform_edt(~corridor)
    else:
        dist_to_corridor = np.full((rows, cols), np.inf)
    keep_clear = half_width_px

    # Distance to the nearest crater cell, so a rock's whole footprint stays out
    # of craters (its centre is already on drivable ground, but a large rock near
    # a rim could otherwise spill in).
    if crater_mask.any():
        dist_to_crater = ndimage.distance_transform_edt(~crater_mask)
    else:
        dist_to_crater = np.full((rows, cols), np.inf)

    drivable_px = int(region.sum())
    requested = estimate_rock_count(params, drivable_px * mean_res ** 2)
    target = min(requested, MAX_ROCKS)

    if target == 0 or drivable_px == 0:
        return np.empty((0, 3)), occupancy, requested

    rng = np.random.default_rng(seed)
    registry = np.empty((target, 3), dtype=float)
    placed, attempts = 0, 0
    max_attempts = target * 20
    cell_taken = np.zeros((rows, cols), dtype=bool)
    large = []          # registry rows of rocks too big to fit in one cell

    # Candidate cells: drivable and already clear of the corridor keep-out.
    candidate_rows, candidate_cols = np.nonzero(region & (dist_to_corridor > keep_clear))
    if candidate_rows.size == 0:
        return np.empty((0, 3)), occupancy, requested

    progress and progress(f"Placing up to {target} rocks", 0.6)

    while placed < target and attempts < max_attempts:
        attempts += 1
        if cancel_check and attempts % 1024 == 0 and cancel_check():
            raise TerrainCancelled()

        pick = rng.integers(candidate_rows.size)
        r, c = int(candidate_rows[pick]), int(candidate_cols[pick])
        radius = min_r_px + (max_r_px - min_r_px) * (rng.random() ** 3)

        # Maximum in-cell offset, and the resulting reach from the cell centre.
        jitter = max(0.0, _CELL_FIT - radius)
        reach = radius + jitter * _SQRT2
        if dist_to_corridor[r, c] <= reach + keep_clear:
            continue
        if dist_to_crater[r, c] <= reach:
            continue

        if jitter > 0:
            if cell_taken[r, c]:
                continue
            # Clamp to the map bounds in edge cells.
            row = rng.uniform(max(r - jitter, 0), min(r + jitter, rows - 1))
            col = rng.uniform(max(c - jitter, 0), min(c + jitter, cols - 1))
            others = registry[large] if large else None
        else:
            row, col = float(r), float(c)
            others = registry[:placed] if placed else None

        # Reject overlaps with rocks already placed (footprints must not touch).
        if others is not None:
            gaps = np.hypot(others[:, 0] - row, others[:, 1] - col)
            if np.any(gaps < others[:, 2] + radius):
                continue

        registry[placed] = (row, col, radius)
        if jitter > 0:
            cell_taken[r, c] = True
            occupancy[r, c] = True          # the cell holds a rock
        else:
            large.append(placed)
            _stamp_disk(occupancy, row, col, radius)
        placed += 1
        if target >= 200 and placed % 200 == 0 and progress:
            progress(f"Placing rocks ({placed}/{target})", 0.6 + 0.25 * placed / target)

    return registry[:placed], occupancy, requested


# =========================================================================
# TOP-LEVEL DRIVER
# =========================================================================

def build_rock_field(elevation, resolution, params, seed=None,
                     progress=None, cancel_check=None):
    """
    Plans a rock field and rover-corridor network for a DEM.

    Args:
        elevation (np.ndarray): 2D elevation map (metres).
        resolution (float): Ground metres per DEM pixel.
        params (dict): Coerced rock-planning parameters (see coerce_params).
        seed (int|None): Random seed for reproducible placement.
        progress (callable|None): fn(message, fraction) status hook.
        cancel_check (callable|None): fn() -> bool cooperative-cancel hook.

    Returns:
        dict describing the plan; see the assembled result at the end.
    """
    def report(message, fraction):
        if cancel_check and cancel_check():
            raise TerrainCancelled()
        if progress:
            progress(message, fraction)

    params = coerce_params(params)
    report("Preparing planning grid", 0.05)
    plan, res_row, res_col, scale_row, scale_col = _plan_grid(
        elevation, resolution, PLAN_GRID_CAP)
    rows, cols = plan.shape
    total_px = rows * cols

    # Craters are the only impassable terrain: closed depressions the rover
    # cannot cross, also off-limits for rocks. Everything else is drivable.
    report("Detecting craters", 0.25)
    crater_mask, depression = detect_craters(plan, params['MIN_CRATER_DEPTH_M'])
    passable = ~crater_mask
    region, _, _ = _largest_component(passable)

    # Cost used only when bridging: ground with more clearance from craters is
    # cheaper (the penalty decays with distance to the nearest crater), so a
    # bridge runs down the middle of the gap it crosses.
    mean_res = 0.5 * (res_row + res_col)
    dist_to_hazard = ndimage.distance_transform_edt(passable)
    decay = max(4.0, 1.5 * params['PATH_WIDTH_M'] / mean_res)
    cost = 1.0 + 6.0 * np.exp(-dist_to_hazard / decay)

    # Roads follow the Voronoi diagram of the craters: each crater is a site and
    # their shared edges are the corridors. Any pieces broken by steep ground are
    # bridged into one connected network. With fewer than two craters there is no
    # diagram, so no corridors are produced.
    voronoi = _voronoi_corridors(region, crater_mask, progress)
    if voronoi is not None:
        corridor, paths, sites = voronoi
        corridor, bridges = _connect_components(cost, region, corridor, progress)
        paths = paths + bridges
        markers = [(int(round(r)), int(round(c))) for r, c in sites]
    else:
        corridor = np.zeros((rows, cols), dtype=bool)
        paths = []
        markers = []
    marker_label = "Crater centre (site)"

    registry, occupancy, requested = _place_rocks(
        region, corridor, crater_mask, params, res_row, res_col, seed,
        progress, cancel_check)

    report("Verifying rover connectivity", 0.9)
    # The corridor is protected from rocks, so it stays drivable; the route is
    # connected when the whole corridor is a single connected component.
    free = region & ~occupancy
    _, region_count = ndimage.label(free, structure=np.ones((3, 3)))
    _, road_count = ndimage.label(corridor, structure=np.ones((3, 3)))
    connected = road_count <= 1

    rocks_world = np.empty((len(registry), 6))  # row, col (full res), x, y, scale, height
    if len(registry):
        rocks_world[:, 0] = np.round(registry[:, 0] * scale_row)
        rocks_world[:, 1] = np.round(registry[:, 1] * scale_col)
        rocks_world[:, 2] = registry[:, 1] * res_col          # x (east), metres
        rocks_world[:, 3] = registry[:, 0] * res_row          # y (north), metres
        rocks_world[:, 4] = registry[:, 2] * mean_res         # scale (metre radius)
        rocks_world[:, 5] = rocks_world[:, 4] * params['ROCK_HEIGHT_RATIO']

    report("Rock field complete", 1.0)
    return {
        'params': params,
        'seed': seed,
        'resolution': resolution,
        'full_shape': elevation.shape,
        'plan_elevation': plan,
        'plan_shape': (rows, cols),
        'plan_res': (res_row, res_col),
        'plan_scale': (scale_row, scale_col),
        'passable': passable,
        'crater_mask': crater_mask,
        'crater_frac': float(crater_mask.sum()) / total_px,
        'region': region,
        'corridor': corridor,
        'paths': paths,
        'markers': markers,
        'marker_label': marker_label,
        'road_segments': len(paths),
        'occupancy': occupancy,
        'rocks_px': registry,
        'rocks_world': rocks_world,
        'connected': connected,
        'free_region_count': int(region_count),
        'drivable_frac': float(region.sum()) / total_px,
        'free_frac': float(free.sum()) / total_px,
        'rock_count': int(len(registry)),
        # Rocks the coverage asked for, and whether MAX_ROCKS cut that short.
        'rock_requested': int(requested),
        'rock_capped': requested > MAX_ROCKS,
    }


# =========================================================================
# SERIALIZATION
# =========================================================================

def export_rock_field(result, output_dir, base_name, export_obstacle_map=False,
                      progress=None, cancel_check=None):
    """
    Writes the rock field to disk under `output_dir/<group>/`, where <group> is
    `base_name` with any `_elevationMap` tag removed (so a DEM and its rock field
    share one folder). Returns a dict of format key -> absolute path.

    Files:
        <group>_rocks.csv       One row per rock: id, row, col, x_m, y_m, scale,
                                height_m. Instance list for Unreal (scale is the
                                uniform mesh scale, i.e. metre radius of a 1 m mesh).
        <group>_roverPaths.csv  Rover corridor polylines: path_id, x_m, y_m.
        <group>_obstacleMap.csv (optional) Full-resolution 0/1 rock footprint mask.
    """
    def report(message, fraction):
        if cancel_check and cancel_check():
            raise TerrainCancelled()
        if progress:
            progress(message, fraction)

    group = base_name.replace('_elevationMap', '')
    target_dir = os.path.join(output_dir, group)
    os.makedirs(target_dir, exist_ok=True)
    written = {}

    report("Writing rock instances", 0.2)
    rocks = result['rocks_world']
    rock_frame = pd.DataFrame({
        'id': np.arange(len(rocks), dtype=int),
        'row': rocks[:, 0].astype(int) if len(rocks) else np.array([], dtype=int),
        'col': rocks[:, 1].astype(int) if len(rocks) else np.array([], dtype=int),
        'x_m': rocks[:, 2] if len(rocks) else np.array([]),
        'y_m': rocks[:, 3] if len(rocks) else np.array([]),
        'scale': rocks[:, 4] if len(rocks) else np.array([]),
        'height_m': rocks[:, 5] if len(rocks) else np.array([]),
    })
    rocks_csv = os.path.join(target_dir, f"{group}_rocks.csv")
    rock_frame.to_csv(rocks_csv, index=False, float_format='%.4f')
    written['rocks_csv'] = rocks_csv

    report("Writing rover corridors", 0.5)
    res_row, res_col = result['plan_res']
    path_ids, xs, ys = [], [], []
    for pid, (rr, cc) in enumerate(result['paths']):
        path_ids.extend([pid] * len(rr))
        xs.extend((cc * res_col).tolist())
        ys.extend((rr * res_row).tolist())
    paths_csv = os.path.join(target_dir, f"{group}_roverPaths.csv")
    pd.DataFrame({'path_id': path_ids, 'x_m': xs, 'y_m': ys}).to_csv(
        paths_csv, index=False, float_format='%.4f')
    written['paths_csv'] = paths_csv

    if export_obstacle_map:
        report("Rasterizing full-resolution obstacle map", 0.75)
        full_rows, full_cols = result['full_shape']
        occ = result['occupancy'].astype(np.uint8)
        plan_rows, plan_cols = result['plan_shape']
        if (plan_rows, plan_cols) != (full_rows, full_cols):
            occ = ndimage.zoom(occ, (full_rows / plan_rows, full_cols / plan_cols),
                               order=0)
            occ = occ[:full_rows, :full_cols]
            pad = ((0, full_rows - occ.shape[0]), (0, full_cols - occ.shape[1]))
            if any(sum(p) for p in pad):
                occ = np.pad(occ, pad, mode='edge')
        obstacle_csv = os.path.join(target_dir, f"{group}_obstacleMap.csv")
        pd.DataFrame(occ).to_csv(obstacle_csv, index=False, header=False, sep=' ')
        written['obstacle_csv'] = obstacle_csv

    report("Export complete", 1.0)
    return written
