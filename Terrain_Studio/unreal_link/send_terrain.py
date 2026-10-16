"""
Landscape Dispatch
------------------
Turns a DEM into an Unreal Landscape: converts it here (heightmap_files), where
numpy is available and fast, then has unreal_landscape build the Landscape in a
running Unreal Editor.
"""

from unreal_connection import run_in_unreal, unreal_call
import os
import sys
import numpy as np
import heightmap_files

# --- Folders ---
# Forward slashes, so the paths survive being written into Python sent to Unreal.
# input_maps/ and outputs/ live at the repository root, two levels up.
BASE_DIRECTORY        = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
PROJECT_ROOT          = os.path.dirname(os.path.dirname(BASE_DIRECTORY)).replace("\\", "/")
BASE_MAPS_DIRECTORY   = os.path.join(PROJECT_ROOT, "input_maps").replace("\\", "/")
BASE_OUTPUT_DIRECTORY = os.path.join(PROJECT_ROOT, "outputs").replace("\\", "/")


def output_group_directory(source_map_filepath):
    """
    Resolves the per-map output folder for a DEM: <root>/outputs/<group>, where
    <group> is the filename without its extension and without the
    '_elevationMap' tag. terrain_core and rock_field derive their folder the
    same way, so a DEM, its rock field, and its .r16 heightmap end up together.
    """
    stem = os.path.splitext(os.path.basename(source_map_filepath))[0]
    group = stem.replace("_elevationMap", "")
    return os.path.join(BASE_OUTPUT_DIRECTORY, group).replace("\\", "/")


def generate_lunar_terrain(input_map_filepath, output_map_directory, input_resolution):
    """
    Converts a DEM for Unreal: reads it, scales it to 16 bits, chooses the
    Landscape layout and writes the converted text, .r16 and .json files.

    Args:
        input_map_filepath (str): Path to the source DEM (.tif, .npy, etc.).
        output_map_directory (str): Destination for the converted .txt, .r16, and .json files.
        input_resolution (float): Spatial resolution in meters per pixel.

    Returns:
        tuple: (landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename, z_offset)
    """

    # =========================================================================
    # 1. READ THE DEM
    # =========================================================================
    raw_elevation_data = heightmap_files.terrain_file_reading(input_map_filepath, input_resolution)

    # =========================================================================
    # 2. CONVERT TO UNREAL'S SCALE
    # =========================================================================
    # 16-bit heights, with the Z-scale and actor height that reproduce them
    file_header, unreal_elevation_data = heightmap_files.convert_to_unreal_format(
        elevation_data=raw_elevation_data,
        interpolation_resolution=input_resolution
    )

    # Every output is named after the DEM.
    filename = os.path.basename(input_map_filepath)
    filename_without_extension = os.path.splitext(filename)[0]
    converted_dem_filepath = os.path.join(output_map_directory, f"{filename_without_extension}_converted.txt")

    # The converted heights as text, with the conversion in its header
    np.savetxt(
        converted_dem_filepath,
        unreal_elevation_data,
        fmt='%s',
        delimiter=' ',
        newline='\n',
        header=file_header,
        footer='',
        comments='',
        encoding=None
    )

    # =========================================================================
    # 3. LANDSCAPE LAYOUT AND .R16 HEIGHTMAP
    # =========================================================================
    header_values = [float(val) for val in file_header.split()]
    dem_width, dem_height, interpolation_resolution, z_axis_scale, z_offset = header_values
    print(f"[*] Landscape Z-scale: {z_axis_scale:.4f}, actor Z: {z_offset:.2f} m")

    landscape_config = heightmap_files.calculate_optimal_landscape_config(dem_width, dem_height)

    heightmap_files.create_unreal_heightmap(
        unreal_elevation_data,
        int(dem_width),
        int(dem_height),
        landscape_config,
        output_map_directory,
        base_filename=filename_without_extension
    )

    heightmap_filename = f"{filename_without_extension}_heightmap.r16"

    return landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename, z_offset


def send_to_unreal(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename,
                   z_offset=0.0, output_directory=None, save_level=True):
    """
    Has a running Unreal Editor build the Landscape from the heightmap
    (unreal_landscape.create_landscape), printing the editor's output; failures
    print a line starting with "[-]". With save_level, Unreal saves the level
    once the Landscape is spawned.
    """
    # The heightmap is in its per-map output folder.
    heightmap_directory = (output_directory or BASE_OUTPUT_DIRECTORY).replace("\\", "/")
    target_r16_path = f"{heightmap_directory}/{heightmap_filename}"
    command = unreal_call(
        f"create_landscape({landscape_config}, {interpolation_resolution}, {z_axis_scale}, "
        f"'{target_r16_path}', {0.0}, {0.0}, {z_offset}, save_level={bool(save_level)})",
        "unreal_level_saver", "unreal_landscape")

    def log(text, level=None):
        # The editor's own output is indented under the studio's status lines.
        status = text.startswith(("[-]", "Searching", "Building")) or "(node " in text
        print(text if status else f"    Unreal: {text}")

    # Shared, already-open discovery session (unreal_connection); also refreshes
    # the project's own copy of the editor watcher (runtime_sync).
    if run_in_unreal(command, log, "Building the Landscape in Unreal"):
        print("[+] Success: the Landscape was built in Unreal.")


# =========================================================================
# COMMAND LINE
# =========================================================================
if __name__ == "__main__":
    # Defaults for a run without arguments
    input_map_filepath   = os.path.join(BASE_MAPS_DIRECTORY, "procedural_lunar_terrain_1_elevationMap.npy").replace("\\", "/")
    output_map_directory = None
    input_resolution     = 1.0

    # Usage: send_terrain.py <map> [output_dir] [resolution]
    if len(sys.argv) >= 2:
        input_map_filepath   = sys.argv[1].replace("\\", "/")
    if len(sys.argv) >= 3:
        output_map_directory = sys.argv[2].replace("\\", "/")
    if len(sys.argv) >= 4:
        input_resolution     = float(sys.argv[3])

    # Default the destination to this map's folder under the root outputs dir.
    if not output_map_directory:
        output_map_directory = output_group_directory(input_map_filepath)

    os.makedirs(output_map_directory, exist_ok=True)

    print("\n========================================================")
    print(" LUNAR TERRAIN GENERATOR - INITIATING PIPELINE")
    print("========================================================\n")

    # Convert here, then build the Landscape in Unreal.
    landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename, z_offset = generate_lunar_terrain(
        input_map_filepath,
        output_map_directory,
        input_resolution
    )

    send_to_unreal(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename,
                   z_offset=z_offset, output_directory=output_map_directory)