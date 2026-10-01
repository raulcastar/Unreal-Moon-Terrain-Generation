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
UNREAL_SEA_LEVEL = 32768             # 16-bit value Unreal places at the Landscape actor's Z location
UNREAL_HEIGHT_UNITS_PER_SCALE = 128.0  # Engine-hardcoded: one Z-scale unit equals 128 height steps (LANDSCAPE_ZSCALE = 1/128)
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
        # Accept both comma- and whitespace-separated grids (the procedural
        # generator writes space-separated values despite the .csv extension)
        with open(input_filepath, 'r') as f:
            first_line = f.readline()
        delimiter = "," if "," in first_line else None
        raw_elevation_data = np.loadtxt(input_filepath, delimiter=delimiter, skiprows=0)
        
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
        elevation_data (np.ndarray): The raw elevation matrix in meters.
        interpolation_resolution (float): Meters per pixel.

    Returns:
        tuple: (file_header string "width height resolution z_scale z_offset_m",
                integer ndarray in the 0-65535 range)
    """
    # Float copy: integer DEMs cannot hold NaN, and the caller's array must not
    # be modified.
    elevation_data = np.array(elevation_data, dtype=float)

    # Isolate missing data (0 or non-finite) so it cannot skew the height range
    elevation_data[(elevation_data == 0) | ~np.isfinite(elevation_data)] = np.nan

    # Convert real-world metric data to Unreal's native centimeter scale
    elevation_cm = elevation_data * CM_PER_METER

    if np.all(np.isnan(elevation_cm)):
        min_elevation_cm = max_elevation_cm = 0.0
    else:
        min_elevation_cm = float(np.nanmin(elevation_cm))
        max_elevation_cm = float(np.nanmax(elevation_cm))

    # Guard against flat maps: a zero Z-scale would collapse the Landscape
    elevation_span_cm = max(max_elevation_cm - min_elevation_cm, 1.0)

    # The full 16-bit range spans 512 * ScaleZ cm, so one step is ScaleZ / 128 cm
    z_axis_scale = elevation_span_cm * UNREAL_HEIGHT_UNITS_PER_SCALE / UINT16_MAX

    # Value 0 sits 256 * ScaleZ cm below the actor; place it at the lowest elevation
    z_offset_m = (min_elevation_cm + UNREAL_SEA_LEVEL / UNREAL_HEIGHT_UNITS_PER_SCALE * z_axis_scale) / CM_PER_METER

    # Missing data is flattened to the lowest valid elevation
    elevation_cm = np.nan_to_num(elevation_cm, nan=min_elevation_cm)

    normalized_elevation = (elevation_cm - min_elevation_cm) / elevation_span_cm * UINT16_MAX
    unreal_elevation_data = np.clip(np.rint(normalized_elevation), 0, UINT16_MAX).astype(int)

    file_header = (f"{elevation_data.shape[1]} {elevation_data.shape[0]} "
                   f"{interpolation_resolution} {z_axis_scale} {z_offset_m}")

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
    Pads the data matrix to match the final Landscape components and exports the native Little-Endian .r16 binary file.

    Raises:
        ValueError: if no Landscape topology fits the DEM (more than 50x50
            components).
    """
    if landscape_config is None:
        raise ValueError(
            f"No Unreal Landscape fits a {dem_width} x {dem_height} px DEM: it "
            f"needs more than the 50x50 components a single Landscape allows. "
            f"Downsample the map, crop it, or split it across World Partition "
            f"tiles, then convert again.")

    optimal_comp_x, optimal_comp_y, quads, sections, final_width, final_height = landscape_config

    print(f"[+] Optimal Setup Found: {quads} Quads, {sections} Sections.")
    print(f"[*] Component Layout: {optimal_comp_x}x by {optimal_comp_y}y")
    print(f"[*] Final Vertices: {final_width}x by {final_height}y (DEM bounds: {dem_width}x by {dem_height}y)")

    # =========================================================================
    # 4. PREPARE ELEVATION DATA FOR 16-BIT EXPORT
    # =========================================================================
    print("[*] Padding and casting elevation data to 16-bit format...")
    
    # Pad the array edges to prevent "cliff" artifacts and align exactly with the Landscape vertices
    pad_y = max(0, final_height - dem_height)
    pad_x = max(0, final_width - dem_width)
    padded_grid = np.pad(unreal_elevation_data, ((0, pad_y), (0, pad_x)), mode='edge')

    # convert_to_unreal_format already spans the full 0-65535 range, and the
    # Landscape Z-scale is derived from it, so the values must not be rescaled here.
    heightmap_data = np.clip(padded_grid, 0, UINT16_MAX).astype('<u2')
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