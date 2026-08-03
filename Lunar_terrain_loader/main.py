"""
Unreal Engine Remote Execution Client
-------------------------------------
Acts as the master orchestrator for the terrain generation pipeline.
Executes the heavy data processing locally to bypass Unreal's internal Python limitations,
then establishes a network connection to Unreal Engine to trigger the landscape compilation.
"""

import unreal_api
import time
import os
import sys
import numpy as np
import terrain_files_generator

# --- Global Directory Configuration ---
# Absolute paths are normalized with forward slashes to prevent escape character 
# errors when passing these string paths over the network to Unreal Engine.
BASE_DIRECTORY        = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
BASE_MAPS_DIRECTORY   = os.path.join(BASE_DIRECTORY, "maps").replace("\\", "/")
BASE_OUTPUT_DIRECTORY = os.path.join(BASE_DIRECTORY, "outputs").replace("\\", "/")


def generate_lunar_terrain(input_map_filepath, output_map_directory, input_resolution):
    """
    Manages the local data processing pipeline: reads the raw DEM, normalizes it 
    for Unreal Engine, calculates component topology, and exports the native binary files.
    
    Args:
        input_map_filepath (str): Path to the source DEM (.tif, .npy, etc.).
        output_map_directory (str): Destination for the converted .txt, .r16, and .json files.
        input_resolution (float): Spatial resolution in meters per pixel.
        
    Returns:
        tuple: (landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename)
    """
    
    # =========================================================================
    # 1. READ ORIGINAL DEM
    # =========================================================================
    raw_elevation_data = terrain_files_generator.terrain_file_reading(input_map_filepath, input_resolution)
    
    # =========================================================================
    # 2. UNREAL FORMAT CONVERSION
    # =========================================================================
    # Translate metric data to Unreal Engine scalar space and baseline (Z=32767)
    file_header, unreal_elevation_data = terrain_files_generator.convert_to_unreal_format(
        elevation_data=raw_elevation_data, 
        interpolation_resolution=input_resolution
    )

    # Isolate the core filename to dynamically name all subsequent exported assets
    filename = os.path.basename(input_map_filepath)
    filename_without_extension = os.path.splitext(filename)[0]
    converted_dem_filepath = os.path.join(output_map_directory, f"{filename_without_extension}_converted.txt")
    
    # Cache the structured data locally
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
    # 3. CALCULATE TOPOLOGY & EXPORT BINARY
    # =========================================================================
    # Extract dimensions from the cached header
    header_values = [float(val) for val in file_header.split()]
    dem_width, dem_height, interpolation_resolution, z_axis_scale = header_values
    
    # Determine the most memory-efficient landscape component grid
    landscape_config = terrain_files_generator.calculate_optimal_landscape_config(dem_width, dem_height)
    
    # Generate the 16-bit Little-Endian heightmap required by Unreal's importer
    terrain_files_generator.create_unreal_heightmap(
        unreal_elevation_data, 
        int(dem_width), 
        int(dem_height), 
        landscape_config, 
        output_map_directory,
        base_filename=filename_without_extension 
    )
    
    # Construct the exact resulting filename to pass to the Unreal receiver script
    heightmap_filename = f"{filename_without_extension}_heightmap.r16"

    return landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename


def send_to_unreal(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename):
    """
    Initializes a network broadcast to locate a running Unreal Editor, connects to 
    the first available node, and injects the Python execution command.
    """
    
    # Initialize the Unreal Remote Execution network client
    remote_exec = unreal_api.RemoteExecution()
    remote_exec.start()
    
    print(f"[*] Searching for Unreal Engine instances... (Root: {BASE_DIRECTORY})")
    
    # Allow network discovery time to populate the remote_nodes dictionary
    time.sleep(2.0) 

    nodes = remote_exec.remote_nodes
    if nodes and len(nodes) > 0:
        first_node = nodes[0]
        
        # Safely extract the Node ID regardless of the returned data structure
        node_id = first_node.get("node_id") if isinstance(first_node, dict) else getattr(first_node, "node_id", None)

        if node_id:
            # Establish a direct command socket to the target Engine instance
            remote_exec.open_command_connection(node_id)
            
            # Construct the absolute path for the targeted binary file
            target_r16_path = f"{BASE_OUTPUT_DIRECTORY}/{heightmap_filename}"
            
            # --- Remote Command Construction ---
            # Append local directory to Unreal's sys.path, reload the target module to ensure 
            # the latest code is used, and trigger the Landscape creation API.
            command = (
                f"import sys; "
                f"sys.path.append('{BASE_DIRECTORY}') if '{BASE_DIRECTORY}' not in sys.path else None; "
                f"import unreal_terrain_generator; "
                f"import importlib; importlib.reload(unreal_terrain_generator); "
                f"unreal_terrain_generator.create_landscape({landscape_config}, {interpolation_resolution}, {z_axis_scale}, '{target_r16_path}')"
            )
            
            remote_exec.run_command(command)
            print(f"[+] Success: Command dispatched to Node [{node_id}]")
    else:
        print("[-] Error: No Unreal instance detected. Ensure 'Remote Execution' is active in Project Settings.")

    # Terminate the network client to free local ports
    remote_exec.stop()


# =========================================================================
# EXECUTION ENTRY POINT 
# =========================================================================
if __name__ == "__main__":
    # Ensure the target export directory exists before execution to prevent I/O crashes
    if not os.path.exists(BASE_OUTPUT_DIRECTORY):
        os.makedirs(BASE_OUTPUT_DIRECTORY)

    # Establish fallback defaults for direct script execution
    input_map_filepath   = os.path.join(BASE_MAPS_DIRECTORY, "procedural_lunar_terrain_1_elevationMap.npy").replace("\\", "/") 
    output_map_directory = BASE_OUTPUT_DIRECTORY 
    input_resolution     = 1.0

    # Override defaults if executed with arguments via CLI (e.g., from a batch file or UI tool)
    if len(sys.argv) >= 4:
        input_map_filepath   = sys.argv[1].replace("\\", "/")
        output_map_directory = sys.argv[2].replace("\\", "/")
        input_resolution     = float(sys.argv[3])

    print("\n========================================================")
    print(" LUNAR TERRAIN GENERATOR - INITIATING PIPELINE")
    print("========================================================\n")

    # Step 1: Execute local array math and binary generation
    landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename = generate_lunar_terrain(
        input_map_filepath, 
        output_map_directory, 
        input_resolution
    )
    
    # Step 2: Dispatch the compiled configuration to the Unreal Engine host
    send_to_unreal(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filename)