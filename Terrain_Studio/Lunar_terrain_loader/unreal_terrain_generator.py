"""
Unreal Engine Landscape Instantiator
------------------------------------
Executes natively within the Unreal Engine Python environment. 
Receives processed DEM (Digital Elevation Model) topology from the local 
Python client and orchestrates the generation of the Landscape Actor in the current level.
"""

import os

import unreal

from unreal_level_saver import save_current_level

# --- Global Constants ---
# Unreal Engine uses Centimeters (cm) as its native base unit for all world transforms.
CM_PER_METER = 100.0 

def create_landscape(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filepath="outputs/lunar_terrain.r16", x_offset=0.0, y_offset=0.0, z_offset=0.0, save_level=True):
    """
    Instantiates and configures a Landscape actor based on pre-calculated DEM dimensions.
    
    Args:
        landscape_config (tuple): Topology parameters (comp_x, comp_y, quads, sections, width, height).
        interpolation_resolution (float): Spatial resolution of the source DEM (meters per pixel).
        z_axis_scale (float): Normalized vertical scaling factor for the Landscape transform.
        heightmap_filepath (str): Absolute path to the generated .r16 file for user reference.
        x_offset, y_offset, z_offset (float): World-space spawn coordinates in meters.
        save_level (bool): Save the level once the Landscape actor exists.
    """
    
    # Unpack the pre-calculated component topology configuration
    optimal_comp_x, optimal_comp_y, quads, sections, final_width, final_height = landscape_config
    
    # Extract the base filename (e.g., 'lunar_terrain' from 'outputs/lunar_terrain.r16')
    base_filename = os.path.basename(heightmap_filepath)
    actor_label = os.path.splitext(base_filename)[0]
    
    unreal.log(f"[*] Spawning Landscape: {optimal_comp_x}x{optimal_comp_y} components, {quads} quads")

    # Compute physical scale multipliers (Meter to Centimeter conversion).
    # Note: Float casting is strictly required here to prevent truncation of sub-centimeter resolutions (e.g., 0.015).
    unreal_map_scale_x = float(CM_PER_METER * abs(interpolation_resolution))
    unreal_map_scale_y = float(CM_PER_METER * abs(interpolation_resolution))

    # Physical width of the map in meters: N vertices span N - 1 quads.
    map_width_meters = (final_width - 1) * abs(interpolation_resolution)

    # Shift the X offset in the negative direction by the width of the map.
    # The heightmap is mirrored in X (fliplr), so DEM column j sits at vertex
    # final_width - 1 - j; with this origin it lands at X = -j * spacing.
    x_offset -= map_width_meters

    # =========================================================================
    # 1. INSTANTIATE LANDSCAPE ACTOR IN UNREAL
    # =========================================================================
    
    # Construct the foundational world transform for the Landscape actor
    landscape_transform = unreal.Transform(
        location=[x_offset * CM_PER_METER, y_offset * CM_PER_METER, z_offset * CM_PER_METER], 
        rotation=[0.0, 0.0, 0.0], 
        scale=[unreal_map_scale_x, unreal_map_scale_y, z_axis_scale]
    )
    
    # Dispatch the spawn command to the Editor via the TAPython Landscape API
    landscape_actor = unreal.PythonLandscapeLib.create_landscape(
        landscape_transform, 
        section_size=quads, 
        sections_per_component=sections, 
        component_count_x=optimal_comp_x, 
        component_count_y=optimal_comp_y
    )
    
    landscape_actor.set_actor_label(actor_label)

    # Read the transform back so the Output Log shows what the Editor applied
    applied_scale = landscape_actor.get_actor_scale3d()
    applied_location = landscape_actor.get_actor_location()
    relief_m = z_axis_scale * 512.0 / CM_PER_METER
    unreal.log(f"[*] Landscape scale applied: X={applied_scale.x:.4f} Y={applied_scale.y:.4f} Z={applied_scale.z:.4f} "
               f"(requested Z={z_axis_scale:.4f}); location Z={applied_location.z:.2f} cm; "
               f"full 16-bit range spans {relief_m:.2f} m")

    # Enable Nanite on the Landscape (Requires UE 5.3+)
    try:
        landscape_actor.set_editor_property("bEnableNanite", True)
        unreal.log(f"[*] Nanite enabled for Landscape '{actor_label}'.")
    except Exception as e:
        unreal.log_warning(f"[*] Could not enable Nanite. Ensure you are using UE 5.3+. Error: {e}")

    # =========================================================================
    # 2. UPDATE VIEWPORT AND EDITOR STATE
    # =========================================================================
    
    # Force the Editor selection state to the newly created actor and frame the viewport camera
    unreal.PythonBPLib.select_actor(landscape_actor, selected=True, notify=True)
    unreal.PythonBPLib.request_viewport_focus_on_selection()

    # Save now so the Landscape actor survives a restart; the heightmap import
    # below is manual, so the level needs saving again after it.
    if save_level:
        unreal.log(f"[*] {save_current_level()}")
    
    # =========================================================================
    # 3. PROMPT USER WITH IMPORT INSTRUCTIONS
    # =========================================================================
    
    dialog_title = "Landscape Generation Complete"
    
    # Format the dialog message, dynamically injecting the target heightmap path for user reference
    dialog_message = (
        "The Landscape actor has been successfully generated with precise physical scaling.\n\n"
        "To apply the generated heightmap data, please proceed with the following steps:\n\n"
        "1. Enter Landscape Mode (SHIFT + 2).\n"
        "2. Navigate to the 'Manage' tab and ensure the new Landscape is selected.\n"
        "3. Right-click the Landscape in the component list and select 'Import'.\n"
        f"4. Target file:\n   {heightmap_filepath}\n"
        "5. Click 'Import' in the bottom right panel.\n\n"
        "The terrain relief will be applied immediately."
    )
    if save_level:
        dialog_message += ("\n\nThe level has been saved with the flat Landscape: "
                           "save it again (Ctrl+S) after the import.")

    # Suspend Editor execution and visually notify the user of pending manual operations
    unreal.EditorDialog.show_message(
        dialog_title, dialog_message, unreal.AppMsgType.OK, unreal.AppReturnType.OK
    )