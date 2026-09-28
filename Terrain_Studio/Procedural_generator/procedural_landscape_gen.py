"""
Command-line front-end for the procedural lunar terrain pipeline.

The generation logic lives in `terrain_core`, shared with the GUI
(`../terrain_studio.py`). This module handles configuration loading,
file output, and an optional Matplotlib preview.
"""

import argparse
import os

import matplotlib.pyplot as plt
import numpy as np

import terrain_core

# =========================================================================
# CONFIGURATION
# =========================================================================

# Everything the studio produces goes to one folder at the repository root, so
# the CLI defaults there too rather than to a directory relative to the shell.
GENERATOR_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(GENERATOR_DIR))
DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "outputs")
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "settings.yaml")

parser = argparse.ArgumentParser(description="Procedural Lunar Terrain Generator")
parser.add_argument(
    "-c", "--config",
    type=str,
    default=DEFAULT_CONFIG_PATH,
    help="Path to the YAML configuration file (default: <repo root>/config/settings.yaml)"
)
parser.add_argument(
    "-o", "--output-dir",
    type=str,
    default=DEFAULT_OUTPUT_DIR,
    help="Destination directory for the serialized DEM (default: <repo root>/outputs)"
)
parser.add_argument(
    "-n", "--name",
    type=str,
    default="generated_elevationMap_moon",
    help="Base filename for the exported elevation map"
)
parser.add_argument(
    "--no-preview",
    action="store_true",
    help="Skip the Matplotlib preview window (useful for batch runs)"
)
args = parser.parse_args()

CONFIG_PATH = args.config
OUTPUT_DIR = args.output_dir

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Write a default configuration template if the target file is missing.
if not os.path.exists(CONFIG_PATH):
    print(f"[*] Config not found; writing default to '{CONFIG_PATH}'")
    terrain_core.write_default_config(CONFIG_PATH)

print(f"[*] Loading parameters from '{CONFIG_PATH}'")
config = terrain_core.load_config(CONFIG_PATH)


# =========================================================================
# PIPELINE
# =========================================================================

def log_progress(message, fraction):
    """Prints pipeline status to stdout."""
    print(f"[*] {message}")


def generate_and_save_data():
    """Runs the pipeline, writes the outputs, and returns the elevation matrix."""
    seed = config['RANDOM_SEED']
    print(f"[*] Seed: {seed}" if seed is not None else "[*] Seed: random")

    result = terrain_core.generate_terrain(config, progress=log_progress)

    terrain_core.export_outputs(
        result['elevation'],
        output_dir=OUTPUT_DIR,
        base_name=args.name,
        export_csv=config['EXPORT_CSV'],
        export_npy=config['EXPORT_NPY'],
        progress=log_progress,
    )

    print(f"[+] Done. Craters placed: {result['crater_count']}")
    return result['elevation']


def preview_terrain(elevation_matrix):
    """Shows a 2D contour map and a 3D surface of the elevation matrix."""
    print("[*] Opening preview window (close to exit).")

    rows, cols = elevation_matrix.shape
    X, Y = np.meshgrid(np.arange(cols), np.arange(rows))

    fig = plt.figure(figsize=(14, 6))

    ax1 = fig.add_subplot(121)
    contour = ax1.contourf(X, Y, elevation_matrix, cmap='magma', levels=50)
    fig.colorbar(contour, ax=ax1, shrink=0.7, label='Elevation (m)')
    ax1.set_title('2D Topography')
    ax1.set_aspect('equal')

    ax2 = fig.add_subplot(122, projection='3d')
    ax2.plot_surface(X, Y, elevation_matrix, cmap='magma', edgecolor='none',
                     alpha=0.9, rcount=150, ccount=150)
    ax2.set_title('3D Surface')

    ax2.view_init(elev=45, azim=-45)
    z_min, z_max = np.min(elevation_matrix), np.max(elevation_matrix)
    z_range = max(1.0, z_max - z_min)
    ax2.set_zlim(z_min - (z_range * 0.5), z_max + (z_range * 0.5))

    plt.tight_layout()
    plt.show()


# =========================================================================
# ENTRY POINT
# =========================================================================

if __name__ == "__main__":
    final_elevation_matrix = generate_and_save_data()
    if not args.no_preview:
        preview_terrain(final_elevation_matrix)
