"""
Local Terrain Data Processor
----------------------------
Handles the ingestion, scaling, and formatting of real-world Digital Elevation Models (DEM).
Prepares data offline to be dispatched to Unreal Engine via Remote Execution.
"""

import os
import numpy as np
import math
import json

# --- Optional Dependency: tifffile ---
# Attempts to load 'tifffile' for C++-free GeoTIFF support. 
# Falls back gracefully for standard array formats (.npy, .csv) if unavailable.
try:
    import tifffile
    TIFFFILE_AVAILABLE = True
except ImportError:
    TIFFFILE_AVAILABLE = False
    print("[-] 'tifffile' not found. Processing .tif files will not be supported.")

# --- Global Engine Constants ---
UNREAL_SEA_LEVEL = 32767             # 16-bit midpoint (0-65535) used as the Z-axis baseline (sea level)
UNREAL_Z_SCALE_DIVISOR = 256.0       # Engine-hardcoded divisor for Landscape Z-scale calculations
MAX_COMPONENT_GRID_SIZE = 50         # Maximum recommended components to prevent Engine instability
UINT16_MAX = 65535                   # Maximum theoretical value for a 16-bit unsigned integer
CM_PER_METER = 100.0                 # Unreal Engine operates strictly in Centimeters


def terrain_file_reading(input_filepath, spatial_resolution):
    """
    Ingests elevation data from various file formats and extracts spatial metadata.
    
    Args:
        input_filepath (str): Absolute path to the source DEM file.
        spatial_resolution (float): Fallback resolution in meters per pixel.
        
    Returns:
        np.ndarray: A 2D float32 array representing raw elevation data.
    """
    _, file_extension = os.path.splitext(input_filepath)
    file_extension = file_extension.lower()
    
    print(f"[*] Initializing DEM ingestion for file: {input_filepath}")
    
    if file_extension == '.npy':
        raw_elevation_data = np.load(input_filepath)
        
    elif file_extension in ['.csv', '.txt']:
        raw_elevation_data = np.loadtxt(input_filepath, delimiter=",", skiprows=0)
        
    elif file_extension in ['.tif', '.tiff']:
        if not TIFFFILE_AVAILABLE:
           print("[-] Process Terminated: 'tifffile' library is required for .tif files. Please run: pip install tifffile")
           return
            
        print("[*] GeoTIFF detected. Utilizing 'tifffile' to parse elevation matrix...")
        
        try:
            with tifffile.TiffFile(input_filepath) as tif:
                raw_elevation_data = tif.asarray().astype(np.float32)
                
                # Attempt to dynamically override the UI resolution with native TIFF metadata
                tags = tif.pages[0].tags
                if 'ModelPixelScaleTag' in tags:
                    native_x_res = tags['ModelPixelScaleTag'].value[0]
                    print(f"[+] GeoTIFF Native Spatial Resolution detected: {native_x_res}")
                    spatial_resolution = native_x_res
                
                # Sanitize 'NoData' metadata tags to prevent deep vertical spikes
                if 'GDAL_NODATA' in tags:
                    ndv = float(tags['GDAL_NODATA'].value)
                    print(f"[*] Sanitizing 'NoData' values (identified as {ndv})")
                    raw_elevation_data[raw_elevation_data == ndv] = np.nan
                    
        except Exception as e:
            print(f"[-] Process Terminated: Failed to read GeoTIFF. Error: {e}")
            return
            
    else:
        print(f"[-] Process Terminated: Unsupported file extension '{file_extension}'.")
        return
    
    print(f"[*] Proceeding with spatial resolution: {spatial_resolution}")
    return raw_elevation_data


def convert_to_unreal_format(elevation_data, interpolation_resolution=1.0):
    """
    Translates metric elevation data into Unreal Engine's native 16-bit scalar space.
    
    Args:
        elevation_data (np.ndarray): The raw elevation matrix.
        interpolation_resolution (float): Meters per pixel.
        
    Returns:
        tuple: (file_header string, correctly scaled integer ndarray)
    """
    # Isolate missing data to prevent it from skewing the global Z-scale maximums
    elevation_data[elevation_data == 0] = np.nan
    
    # Convert real-world metric data to Unreal's native centimeter scale
    elevation_cm = elevation_data * CM_PER_METER

    max_elevation_cm = np.nanmax(elevation_cm)
    if max_elevation_cm == 0 or np.isnan(max_elevation_cm): 
        max_elevation_cm = 1.0 

    # Calculate exact engine multipliers required to recreate the real-world height
    z_axis_scale = max_elevation_cm / UNREAL_Z_SCALE_DIVISOR
    height_scaling_factor = UNREAL_SEA_LEVEL / max_elevation_cm   
    
    # Scale geometry against the 16-bit midpoint (Unreal's defined Z=0)
    scaled_elevation = (elevation_cm * height_scaling_factor) + UNREAL_SEA_LEVEL
    
    # Flatten anomalous/missing data strictly to the sea-level baseline
    sanitized_elevation = np.nan_to_num(
        scaled_elevation, 
        nan=UNREAL_SEA_LEVEL, 
        posinf=UNREAL_SEA_LEVEL, 
        neginf=UNREAL_SEA_LEVEL
    )
    
    unreal_elevation_data = np.floor(sanitized_elevation).astype(int)
    file_header = f"{elevation_data.shape[1]} {elevation_data.shape[0]} {interpolation_resolution} {z_axis_scale}"

    return file_header, unreal_elevation_data


def calculate_optimal_landscape_config(dem_width, dem_height):
    """
    Iteratively computes the tightest standard Unreal Engine Landscape bounds 
    capable of encompassing the target DEM matrix.
    
    Returns:
        tuple: (comp_x, comp_y, quads, sections, final_width, final_height) or None
    """
    # Strict architectural bounds required by the Engine's Landscape subsystem
    VALID_QUADS = [63, 127, 255]
    VALID_SECTIONS = [1, 2]
    
    best_area = float('inf')
    best_config = None

    for quads in VALID_QUADS:
        for sections in VALID_SECTIONS:
            vertices_per_comp = quads * sections
            
            # Ceiling division guarantees the generated grid fully covers the data
            comp_x = math.ceil((dem_width - 1) / vertices_per_comp)
            comp_y = math.ceil((dem_height - 1) / vertices_per_comp)
            
            # Prune configurations that exceed the engine's stable rendering threshold
            if comp_x > MAX_COMPONENT_GRID_SIZE or comp_y > MAX_COMPONENT_GRID_SIZE:
                continue
                
            predicted_width = (comp_x * vertices_per_comp) + 1
            predicted_height = (comp_y * vertices_per_comp) + 1
            predicted_area = predicted_width * predicted_height
            
            # Persist the configuration yielding the smallest overall memory footprint
            if predicted_area < best_area:
                best_area = predicted_area
                best_config = (comp_x, comp_y, quads, sections, predicted_width, predicted_height)

    if not best_config:
        print("[-] DEM dimensions exceed the maximum 50x50 component threshold. Consider world partitioning.")
        return None
        
    return best_config


def create_unreal_heightmap(unreal_elevation_data, dem_width, dem_height, landscape_config, output_map_directory, base_filename="lunar_terrain"):
    """
    Pads the data matrix to match the final Landscape components, normalizes 
    for maximum 16-bit precision, and exports the native Little-Endian .r16 binary file.
    """
    optimal_comp_x, optimal_comp_y, quads, sections, final_width, final_height = landscape_config

    print(f"[+] Optimal Setup Found: {quads} Quads, {sections} Sections.")
    print(f"[*] Component Layout: {optimal_comp_x}x by {optimal_comp_y}y")
    print(f"[*] Final Vertices: {final_width}x by {final_height}y (DEM bounds: {dem_width}x by {dem_height}y)")

    # =========================================================================
    # 4. PREPARE ELEVATION DATA FOR 16-BIT EXPORT
    # =========================================================================
    print("[*] Padding, normalizing, and casting elevation data to 16-bit format...")
    
    # Pad the array edges to prevent "cliff" artifacts and align exactly with the Landscape vertices
    pad_y = max(0, final_height - dem_height)
    pad_x = max(0, final_width - dem_width)
    padded_grid = np.pad(unreal_elevation_data, ((0, pad_y), (0, pad_x)), mode='edge')

    # Normalize data across the full 0-65535 spectrum to maximize 16-bit bit-depth precision, 
    # preventing "terracing" or step artifacts in the final Unreal Engine material.
    min_elevation = np.min(unreal_elevation_data)
    max_elevation = np.max(unreal_elevation_data)
    elevation_range = max(1.0, max_elevation - min_elevation)
    
    normalized_grid = ((padded_grid - min_elevation) / elevation_range) * UINT16_MAX
    heightmap_data = np.clip(normalized_grid, 0, UINT16_MAX).astype('<u2')
    print("[*] Applying Left-Right flip (fliplr) to heightmap data...")
    
    # np.fliplr returns a view, so we use np.ascontiguousarray to ensure 
    # safe and predictable binary I/O when writing to the .r16 file.
    # Unreal flips the heightmap by default, so we pre-flip it here to maintain the original orientation in-engine
    heightmap_data = np.ascontiguousarray(np.fliplr(heightmap_data))

    # =========================================================================
    # 5. EXPORT NATIVE .R16 HEIGHTMAP AND METADATA
    # =========================================================================
    target_heightmap_name = f"{base_filename}_heightmap.r16"
    target_metadata_name  = f"{base_filename}_heightmap.json"
    
    heightmap_filepath = os.path.join(output_map_directory, target_heightmap_name).replace("\\", "/")
    metadata_filepath  = os.path.join(output_map_directory, target_metadata_name).replace("\\", "/")
    
    # Purge existing output to prevent partial binary overwrites or OS file locks
    for file_path in [heightmap_filepath, metadata_filepath]:
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except OSError: pass
    
    try:
        # Export binary directly via memory dump for optimal massive-array I/O performance
        with open(heightmap_filepath, "wb") as binary_file:
            heightmap_data.tofile(binary_file) 
            binary_file.flush()
            os.fsync(binary_file.fileno()) 
        
        file_size_mb = os.path.getsize(heightmap_filepath) / (1024 * 1024)
        print(f"[+] SUCCESS: R16 file generated at {target_heightmap_name}. Size: {file_size_mb:.2f} MB")
        
    except Exception as e:
        print(f"[-] Critical I/O failure while writing the R16 file: {e}")
        
    # Persist JSON metadata to streamline automated imports in the Unreal Editor
    with open(metadata_filepath, "w") as json_file:
        json.dump({"width": final_width, "height": final_height, "bpp": 16}, json_file)