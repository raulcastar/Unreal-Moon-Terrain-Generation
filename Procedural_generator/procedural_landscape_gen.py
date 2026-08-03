"""
Procedural Lunar Terrain Generator & Visualizer
-----------------------------------------------
Command-line front-end for the procedural lunar surface pipeline.

The generation logic itself lives in `terrain_core`, which is shared with the
graphical front-end (`Terrain_Studio/terrain_studio.py`). This module handles
configuration ingestion, file serialization, and the Matplotlib debug preview.
"""

import argparse
import os

import matplotlib.pyplot as plt
import numpy as np

import terrain_core

# =========================================================================
# CONFIGURATION INGESTION & ENVIRONMENT SETUP
# =========================================================================

parser = argparse.ArgumentParser(description="Procedural Lunar Terrain Generator")
parser.add_argument(
    "-c", "--config",
    type=str,
    default="config/settings.yaml",
    help="Path to the YAML configuration file (default: config/settings.yaml)"
)
parser.add_argument(
    "-o", "--output-dir",
    type=str,
    default="outputs",
    help="Destination directory for the serialized DEM (default: outputs)"
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

# Generate a default configuration template if the specified file is missing
if not os.path.exists(CONFIG_PATH):
    print(f"[*] Configuration file not found. Initializing default at '{CONFIG_PATH}'")
    terrain_core.write_default_config(CONFIG_PATH)

print(f"[*] Ingesting parameters from '{CONFIG_PATH}'")
config = terrain_core.load_config(CONFIG_PATH)


# =========================================================================
# PIPELINE EXECUTION
# =========================================================================

def log_progress(message, fraction):
    """Streams pipeline status updates to stdout."""
    print(f"[*] {message}")


def generate_and_save_data():
    """
    Runs the procedural pipeline and serializes the results to disk.

    Returns:
        np.ndarray: The finalized 2D elevation matrix.
    """
    if config['RANDOM_SEED'] is not None:
        print(f"[*] State locked. Using deterministic seed: {config['RANDOM_SEED']}")
    else:
        print("[*] State open. Proceeding with stochastic generation...")

    result = terrain_core.generate_terrain(config, progress=log_progress)

    terrain_core.export_outputs(
        result['elevation'],
        output_dir=OUTPUT_DIR,
        base_name=args.name,
        export_csv=config['EXPORT_CSV'],
        export_npy=config['EXPORT_NPY'],
        progress=log_progress,
    )

    print(f"[+] Pipeline completed successfully. Valid impacts registered: {result['crater_count']}")
    return result['elevation']


def preview_terrain(elevation_matrix):
    """
    Constructs a 2D topographical heatmap and a 3D structural mesh utilizing
    Matplotlib to visually debug the procedural generation outputs.

    Args:
        elevation_matrix (np.ndarray): The finalized 2D elevation matrix to render.
    """
    print("[*] Generating structural preview telemetry... (Terminate window to exit)")

    rows, cols = elevation_matrix.shape
    X, Y = np.meshgrid(np.arange(cols), np.arange(rows))

    fig = plt.figure(figsize=(14, 6))

    # Compile 2D topological contour map
    ax1 = fig.add_subplot(121)
    contour = ax1.contourf(X, Y, elevation_matrix, cmap='magma', levels=50)
    fig.colorbar(contour, ax=ax1, shrink=0.7, label='Elevation (Meters)')
    ax1.set_title('2D Topographical Analysis')
    ax1.set_aspect('equal')

    # Compile 3D structural downsampled surface mesh
    ax2 = fig.add_subplot(122, projection='3d')
    ax2.plot_surface(X, Y, elevation_matrix, cmap='magma', edgecolor='none',
                     alpha=0.9, rcount=150, ccount=150)
    ax2.set_title('3D Structural Preview')

    # Set localized camera view and constraint bounds
    ax2.view_init(elev=45, azim=-45)
    z_min, z_max = np.min(elevation_matrix), np.max(elevation_matrix)
    z_range = max(1.0, z_max - z_min)
    ax2.set_zlim(z_min - (z_range * 0.5), z_max + (z_range * 0.5))

    plt.tight_layout()
    plt.show()


# =========================================================================
# RUNTIME EXECUTION
# =========================================================================

if __name__ == "__main__":
    final_elevation_matrix = generate_and_save_data()
    if not args.no_preview:
        preview_terrain(final_elevation_matrix)
