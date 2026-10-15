"""
Where things are: the repository's config, input_maps and outputs folders,
and the studio's code folders (put on the import path here).
"""

import os
import sys

# =========================================================================
# PROJECT PATHS & SIBLING MODULE IMPORTS
# =========================================================================

STUDIO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # Terrain_Studio/


PROJECT_ROOT = os.path.dirname(STUDIO_DIR)


GENERATOR_DIR = os.path.join(STUDIO_DIR, "generator")         # DEM, rocks, craters, sun maths


UNREAL_LINK_DIR = os.path.join(STUDIO_DIR, "unreal_link")     # Senders that talk to Unreal


# Presets, source DEMs and generated files all sit at the repository root, so
# they are visible without digging into the code folders.
CONFIG_DIR = os.path.join(PROJECT_ROOT, "config")
ROCK_CONFIG_DIR = os.path.join(CONFIG_DIR, "rocks")              # Rock field presets
DEFAULT_ROCK_PRESET = "lunar_mare.yaml"


# All generated files go under outputs/<map name>/ at the repository root.
OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "outputs")


GENERATOR_OUTPUT_DIR = OUTPUT_ROOT


INPUT_MAPS_DIR = os.path.join(PROJECT_ROOT, "input_maps")


def map_browse_dir():
    """Where a DEM picker opens: generated maps first, then the raw input maps."""
    for candidate in (OUTPUT_ROOT, INPUT_MAPS_DIR):
        if os.path.isdir(candidate):
            return candidate
    return PROJECT_ROOT


def output_group_dir(source_path):
    """Per-map output folder: the filename without extension or '_elevationMap'."""
    stem = os.path.splitext(os.path.basename(source_path))[0]
    return os.path.join(OUTPUT_ROOT, stem.replace("_elevationMap", ""))

# The code folders are plain script directories, placed on the import path
# rather than being turned into installable packages.
for _path in (GENERATOR_DIR, UNREAL_LINK_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
