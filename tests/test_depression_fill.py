"""
The depression fill behind crater detection, and the crater search cache.

_fill_depressions uses a compiled greyscale reconstruction; it must give the
same surface as the 4-connected priority flood it replaced (Barnes 2014),
which is kept here as the reference.
"""

import heapq

import numpy as np
import pytest

import crater_map
import rock_field
from terrain_fixtures import crater_dem


def priority_flood(elevation):
    """The previous implementation: border-seeded 4-connected priority flood."""
    rows, cols = elevation.shape
    filled = np.empty_like(elevation, dtype=float)
    closed = np.zeros((rows, cols), dtype=bool)
    heap = []
    for r in range(rows):
        for c in range(cols):
            if r in (0, rows - 1) or c in (0, cols - 1):
                heap.append((float(elevation[r, c]), r, c))
                closed[r, c] = True
                filled[r, c] = elevation[r, c]
    heapq.heapify(heap)
    while heap:
        level, r, c = heapq.heappop(heap)
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not closed[nr, nc]:
                closed[nr, nc] = True
                raised = max(float(elevation[nr, nc]), level)
                filled[nr, nc] = raised
                heapq.heappush(heap, (raised, nr, nc))
    return filled


@pytest.mark.parametrize("seed", range(6))
def test_fill_matches_the_priority_flood(seed):
    rng = np.random.default_rng(seed)
    rows, cols = rng.integers(3, 60, size=2)
    dem = rng.normal(size=(rows, cols)).cumsum(axis=0) + rng.normal(size=(rows, cols))
    np.testing.assert_array_equal(rock_field._fill_depressions(dem), priority_flood(dem))


def test_fill_matches_on_a_cratered_terrain():
    dem = crater_dem()
    np.testing.assert_array_equal(rock_field._fill_depressions(dem), priority_flood(dem))


def test_fill_fills_a_pit_to_its_outlet_and_keeps_a_valley():
    dem = np.full((7, 7), 5.0)
    dem[3, 3] = 1.0                      # closed pit: filled to the rim
    dem[1:6, 0] = 2.0                    # notch on the border: drains, not filled
    filled = rock_field._fill_depressions(dem)
    assert filled[3, 3] == 5.0
    np.testing.assert_array_equal(filled[1:6, 0], 2.0)


def test_find_craters_is_cached_per_terrain():
    dem = crater_dem()
    crater_map._FOUND.clear()
    first = crater_map.find_craters(dem, 1.0, 0.5)
    calls = []
    again = crater_map.find_craters(dem.copy(), 1.0, 0.5, progress=lambda m, f: calls.append(m))
    assert again == first and calls == ["Craters found"]          # served from the cache
    other = dem.copy()
    other[0, 0] += 1.0
    crater_map.find_craters(other, 1.0, 0.5)                       # a different terrain is searched
    assert len(crater_map._FOUND) == 2
    again.append((0.0, 0.0, 1.0))
    assert crater_map.find_craters(dem, 1.0, 0.5) == first         # callers cannot alter the cache
