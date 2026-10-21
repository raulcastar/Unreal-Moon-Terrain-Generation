"""
Unreal Landscape
----------------
Runs inside the Unreal Editor. Spawns a Landscape in the open level with the
layout and scale the studio computed (heightmap_files), imports its .r16
heightmap and gives it its material and object-mask id.

Unreal's Python API cannot create Landscape components, so the Landscape is
created by the UnrealDEMLandscape editor plugin (https://github.com/raulcastar/UnrealDEMLandscape,
unreal.DEMLandscapeLibrary), which imports the heights as it creates it.
"""

import os

import unreal

import mask_classes
from unreal_level_saver import save_current_level

CM_PER_METER = 100.0             # Unreal works in centimetres

# Pixels sampled when checking the merged heightmap.
_SPOT_CHECKS = 64


def landscape_library():
    """unreal.DEMLandscapeLibrary, or RuntimeError if the UnrealDEMLandscape plugin is not loaded."""
    library = getattr(unreal, "DEMLandscapeLibrary", None)
    if library is None:
        raise RuntimeError("the UnrealDEMLandscape plugin is not loaded: enable it in Edit > Plugins "
                           "(the MARE Unreal project includes it) and restart the editor")
    return library

# Material given to every new Landscape, and its id in the rover's object mask
# (Custom Depth Stencil; config/mask_classes.json).
DEFAULT_LANDSCAPE_MATERIAL = "/Game/Landscape/Landscape_Material"
LANDSCAPE_MASK_STENCIL = mask_classes.id_of("landscape")


def read_r16(heightmap_filepath, width, height):
    """Reads a little-endian .r16 heightmap. Raises ValueError if its size does not match."""
    with open(heightmap_filepath, "rb") as handle:
        data = handle.read()
    if len(data) != 2 * width * height:
        raise ValueError(f"{os.path.basename(heightmap_filepath)} holds {len(data) // 2} heights, "
                         f"the Landscape needs {width} x {height} = {width * height}")
    return data


def _r16_height(data, width, x, y):
    index = 2 * (y * width + x)
    return data[index] | (data[index + 1] << 8)


def heightmap_merged(landscape_actor, r16_bytes, width, height, samples=_SPOT_CHECKS):
    """Returns True once the Landscape's final heightmap matches the .r16 at the sampled points."""
    heights = landscape_library().get_heightmap_data(landscape_actor)
    if len(heights) != width * height:
        return False
    for k in range(samples):
        x, y = (k * 7919) % width, (k * 104729) % height
        if heights[y * width + x] != _r16_height(r16_bytes, width, x, y):
            return False
    return True


# Polling interval and timeout, in editor frames, for the edit-layer merge.
_MERGE_CHECK_FRAMES = 10
_MERGE_TIMEOUT_FRAMES = 1200


def save_when_heightmap_merged(landscape_actor, heightmap_filepath, width, height):
    """
    Saves the level once the edit-layer merge has applied the imported heights.
    The merge completes on later editor ticks, so the check runs in a Slate
    post-tick callback.
    """
    data = read_r16(heightmap_filepath, width, height)
    state = {"frames": 0, "handle": None}

    def on_tick(_delta_seconds):
        state["frames"] += 1
        if state["frames"] % _MERGE_CHECK_FRAMES:
            return
        try:
            merged = heightmap_merged(landscape_actor, data, width, height)
        except Exception as error:  # e.g. Landscape deleted before the merge completed
            unreal.unregister_slate_post_tick_callback(state["handle"])
            unreal.log_warning(f"[*] Level not saved: {error}")
            return
        if merged:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            unreal.log(f"[*] Heightmap merged after {state['frames']} frames. {save_current_level()}")
        elif state["frames"] >= _MERGE_TIMEOUT_FRAMES:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            unreal.log_warning("[*] Level not saved: the heightmap did not show on the Landscape. "
                               "Check it and save the level yourself.")

    state["handle"] = unreal.register_slate_post_tick_callback(on_tick)


def setup_landscape(landscape_actor, material_path=DEFAULT_LANDSCAPE_MATERIAL,
                    mask_stencil=LANDSCAPE_MASK_STENCIL):
    """
    Gives a Landscape its material and its object-mask id. A missing material
    is logged and left as the engine default; a stencil of 0 leaves the mask off.
    """
    if material_path:
        material = unreal.load_asset(material_path)
        if material is None:
            unreal.log_warning(f"[*] Landscape material {material_path} not found; "
                               "the Landscape keeps the default material.")
        else:
            landscape_actor.set_editor_property("landscape_material", material)
            unreal.log(f"[*] Landscape material: {material_path}")
    if mask_stencil:
        landscape_actor.set_editor_property("render_custom_depth", True)
        landscape_actor.set_editor_property("custom_depth_stencil_value", int(mask_stencil))
        unreal.log(f"[*] Landscape object-mask id (Custom Depth Stencil): {int(mask_stencil)}")


def create_landscape(landscape_config, interpolation_resolution, z_axis_scale, heightmap_filepath="outputs/lunar_terrain.r16", x_offset=0.0, y_offset=0.0, z_offset=0.0, save_level=True, import_heightmap=True,
                     landscape_material=DEFAULT_LANDSCAPE_MATERIAL, mask_stencil=LANDSCAPE_MASK_STENCIL):
    """
    Spawns the Landscape for a converted DEM, imports its heightmap and sets it up.

    Args:
        landscape_config (tuple): Topology parameters (comp_x, comp_y, quads, sections, width, height).
        interpolation_resolution (float): Spatial resolution of the source DEM (meters per pixel).
        z_axis_scale (float): Landscape Z-scale (heightmap_files.convert_to_unreal_format).
        heightmap_filepath (str): Absolute path to the .r16 heightmap.
        x_offset, y_offset, z_offset (float): World-space spawn coordinates in meters.
        save_level (bool): Save the level once the Landscape is set up.
        import_heightmap (bool): Import the .r16 automatically. The manual import
            instructions are shown only if this is disabled or fails.
        landscape_material (str): Material asset applied to the Landscape ('' keeps
            the engine default).
        mask_stencil (int): Custom Depth Stencil id for the object mask (0 = off).
    """

    optimal_comp_x, optimal_comp_y, quads, sections, final_width, final_height = landscape_config

    # Labelled after the heightmap ('lunar_terrain' for outputs/lunar_terrain.r16).
    base_filename = os.path.basename(heightmap_filepath)
    actor_label = os.path.splitext(base_filename)[0]

    unreal.log(f"[*] Spawning Landscape: {optimal_comp_x}x{optimal_comp_y} components, {quads} quads")

    # Cell size in cm (float, so a resolution like 0.015 m is not truncated).
    unreal_map_scale_x = float(CM_PER_METER * abs(interpolation_resolution))
    unreal_map_scale_y = float(CM_PER_METER * abs(interpolation_resolution))

    # Physical width of the map in meters: N vertices span N - 1 quads.
    map_width_meters = (final_width - 1) * abs(interpolation_resolution)

    # Shift the X offset in the negative direction by the width of the map.
    # The heightmap is mirrored in X (fliplr): DEM column j maps to vertex
    # final_width - 1 - j, i.e. X = -j * spacing with this origin.
    x_offset -= map_width_meters

    # =========================================================================
    # 1. SPAWN THE LANDSCAPE WITH ITS HEIGHTMAP
    # =========================================================================

    landscape_transform = unreal.Transform(
        location=[x_offset * CM_PER_METER, y_offset * CM_PER_METER, z_offset * CM_PER_METER],
        rotation=[0.0, 0.0, 0.0],
        scale=[unreal_map_scale_x, unreal_map_scale_y, z_axis_scale]
    )

    # The heights are imported as the Landscape is created; an unreadable or
    # mis-sized .r16 gives a flat Landscape and the manual import instructions.
    problem = None if import_heightmap else "automatic import is turned off"
    if problem is None:
        try:
            read_r16(heightmap_filepath, final_width, final_height)
        except (OSError, ValueError) as error:
            problem = str(error)

    landscape_actor = landscape_library().create_landscape(
        landscape_transform,
        section_size=quads,
        sections_per_component=sections,
        component_count_x=optimal_comp_x,
        component_count_y=optimal_comp_y,
        heightmap_file=heightmap_filepath if problem is None else ""
    )
    if landscape_actor is None:
        raise RuntimeError("Unreal could not create the Landscape (see the Output Log, LogDEMLandscape)")

    landscape_actor.set_actor_label(actor_label)

    # Read the transform back so the Output Log shows what the Editor applied
    applied_scale = landscape_actor.get_actor_scale3d()
    applied_location = landscape_actor.get_actor_location()
    relief_m = z_axis_scale * 512.0 / CM_PER_METER
    unreal.log(f"[*] Landscape scale applied: X={applied_scale.x:.4f} Y={applied_scale.y:.4f} Z={applied_scale.z:.4f} "
               f"(requested Z={z_axis_scale:.4f}); location Z={applied_location.z:.2f} cm; "
               f"full 16-bit range spans {relief_m:.2f} m")

    # Nanite Landscape (UE 5.3+)
    try:
        landscape_actor.set_editor_property("bEnableNanite", True)
        unreal.log(f"[*] Nanite enabled for Landscape '{actor_label}'.")
    except Exception as e:
        unreal.log_warning(f"[*] Could not enable Nanite. Ensure you are using UE 5.3+. Error: {e}")

    setup_landscape(landscape_actor, landscape_material, mask_stencil)

    if problem is None:
        unreal.log(f"[*] Heightmap applied from {heightmap_filepath}")
    else:
        unreal.log_warning(f"[*] Heightmap not applied: {problem}. Import it manually.")

    # =========================================================================
    # 2. SELECT IT AND SAVE
    # =========================================================================

    # Select the new Landscape and look at it from above, at 45 degrees.
    unreal.get_editor_subsystem(unreal.EditorActorSubsystem).set_selected_level_actors([landscape_actor])
    origin, extent = landscape_actor.get_actor_bounds(False)
    distance = 1.5 * max(extent.x, extent.y, 1.0)
    unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).set_level_viewport_camera_info(
        unreal.Vector(origin.x - distance, origin.y, origin.z + distance), unreal.Rotator(0.0, -45.0, 0.0))

    # Persist the level. After an automatic import the save is deferred until the
    # edit layers have merged; otherwise the flat Landscape is saved immediately.
    if save_level and problem is None:
        save_when_heightmap_merged(landscape_actor, heightmap_filepath, final_width, final_height)
        unreal.log("[*] The level will be saved once the heightmap shows on the Landscape.")
    elif save_level:
        unreal.log(f"[*] {save_current_level()}")

    if problem is None:
        return landscape_actor

    # =========================================================================
    # 3. MANUAL IMPORT INSTRUCTIONS (when the automatic import did not run)
    # =========================================================================

    dialog_title = "Landscape Generation Complete"

    dialog_message = (
        "The Landscape actor has been successfully generated with precise physical scaling.\n\n"
        f"The heightmap was not applied automatically ({problem}).\n"
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

    unreal.EditorDialog.show_message(
        dialog_title, dialog_message, unreal.AppMsgType.OK, unreal.AppReturnType.OK
    )
    return landscape_actor