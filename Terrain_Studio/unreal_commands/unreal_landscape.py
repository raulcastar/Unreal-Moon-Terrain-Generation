"""
Unreal Engine Landscape Instantiator
------------------------------------
Executes natively within the Unreal Engine Python environment. 
Receives processed DEM (Digital Elevation Model) topology from the local 
Python client and orchestrates the generation of the Landscape Actor in the current level.
"""

import os
import struct
import zlib

import unreal

from unreal_level_saver import save_current_level

# --- Global Constants ---
# Unreal Engine uses Centimeters (cm) as its native base unit for all world transforms.
CM_PER_METER = 100.0

# Maximum Landscape size (vertices) for the automatic heightmap import. Larger
# Landscapes fall back to the manual import to bound editor memory use.
AUTO_HEIGHTMAP_MAX_VERTICES = 4097 * 4097

# Pixels sampled when validating the render target and the merged heightmap.
_SPOT_CHECKS = 64

# Material given to every new Landscape, and its id in the rover's object mask
# (Custom Depth Stencil; 10 is 'landscape' in the GNC mask labels).
DEFAULT_LANDSCAPE_MATERIAL = "/Game/Landscape/Landscape_Material"
LANDSCAPE_MASK_STENCIL = 10


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


def heightmap_png(r16_bytes, width, height):
    """
    Encodes the heights as an 8-bit RGBA PNG (R = high byte, G = low byte), the
    layout read by landscape_import_heightmap_from_render_target.
    """
    pixels = width * height
    rgba = bytearray(4 * pixels)
    rgba[0::4] = r16_bytes[1::2]
    rgba[1::4] = r16_bytes[0::2]
    rgba[3::4] = b"\xff" * pixels
    stride = 4 * width
    # Each scanline is prefixed with filter type 0 (None).
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xffffffff))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def apply_heightmap(landscape_actor, heightmap_filepath, width, height):
    """
    Imports the .r16 heightmap into the Landscape's base edit layer.

    UE 5.7 Landscapes always use edit layers and recompute the final heightmap
    from them on later ticks, so the data is imported into the layer through a
    render target rather than written to the final heightmap.

    Returns:
        str | None: None on success, otherwise the reason the import was skipped.
    """
    if width * height > AUTO_HEIGHTMAP_MAX_VERTICES:
        return f"the Landscape is too large ({width} x {height}) to apply from Python"
    try:
        data = read_r16(heightmap_filepath, width, height)
    except (OSError, ValueError) as error:
        return str(error)

    rendering = unreal.RenderingLibrary
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    texture = rendering.import_buffer_as_texture2d(world, list(heightmap_png(data, width, height)))
    if texture is None:
        return "Unreal could not load the heightmap as a texture"
    # Sample raw texel values: linear colour space, nearest filtering.
    texture.set_editor_property("srgb", False)
    texture.set_editor_property("filter", unreal.TextureFilter.TF_NEAREST)

    target = rendering.create_render_target2d(
        world, width, height, unreal.TextureRenderTargetFormat.RTF_RGBA32F)
    try:
        canvas, _size, context = rendering.begin_draw_canvas_to_render_target(world, target)
        canvas.draw_texture(texture, unreal.Vector2D(0, 0), unreal.Vector2D(width, height),
                            unreal.Vector2D(0, 0), unreal.Vector2D(1, 1),
                            unreal.LinearColor(1, 1, 1, 1), unreal.BlendMode.BLEND_OPAQUE)
        rendering.end_draw_canvas_to_render_target(world, context)

        # Validate the render target before modifying the Landscape.
        for k in range(_SPOT_CHECKS):
            x, y = (k * 7919) % width, (k * 104729) % height
            pixel = rendering.read_render_target_raw_pixel(world, target, x, y)
            value = int(round(pixel.r * 255)) * 256 + int(round(pixel.g * 255))
            expected = _r16_height(data, width, x, y)
            if value != expected:
                return (f"the render target does not match the .r16 at ({x}, {y}): "
                        f"{value} instead of {expected}")

        if not landscape_actor.landscape_import_heightmap_from_render_target(target, True, 0):
            return "Unreal did not accept the heightmap"
    finally:
        rendering.release_render_target2d(target)
    return None


def heightmap_merged(landscape_actor, r16_bytes, width, height, samples=_SPOT_CHECKS):
    """Returns True once the Landscape's final heightmap matches the .r16 at the sampled points."""
    heights = unreal.PythonLandscapeLib.get_heightmap_data(landscape_actor)
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
    Instantiates and configures a Landscape actor based on pre-calculated DEM dimensions.
    
    Args:
        landscape_config (tuple): Topology parameters (comp_x, comp_y, quads, sections, width, height).
        interpolation_resolution (float): Spatial resolution of the source DEM (meters per pixel).
        z_axis_scale (float): Normalized vertical scaling factor for the Landscape transform.
        heightmap_filepath (str): Absolute path to the generated .r16 file for user reference.
        x_offset, y_offset, z_offset (float): World-space spawn coordinates in meters.
        save_level (bool): Save the level once the Landscape is set up.
        import_heightmap (bool): Import the .r16 automatically. The manual import
            instructions are shown only if this is disabled or fails.
        landscape_material (str): Material asset applied to the Landscape ('' keeps
            the engine default).
        mask_stencil (int): Custom Depth Stencil id for the object mask (0 = off).
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
    # The heightmap is mirrored in X (fliplr): DEM column j maps to vertex
    # final_width - 1 - j, i.e. X = -j * spacing with this origin.
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

    setup_landscape(landscape_actor, landscape_material, mask_stencil)

    # =========================================================================
    # 2. APPLY THE HEIGHTMAP
    # =========================================================================

    problem = (apply_heightmap(landscape_actor, heightmap_filepath, final_width, final_height)
               if import_heightmap else "automatic import is turned off")
    if problem is None:
        unreal.log(f"[*] Heightmap applied from {heightmap_filepath}")
    else:
        unreal.log_warning(f"[*] Heightmap not applied: {problem}. Import it manually.")

    # =========================================================================
    # 3. UPDATE VIEWPORT AND EDITOR STATE
    # =========================================================================

    # Force the Editor selection state to the newly created actor and frame the viewport camera
    unreal.PythonBPLib.select_actor(landscape_actor, selected=True, notify=True)
    unreal.PythonBPLib.request_viewport_focus_on_selection()

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
    # 4. PROMPT USER WITH IMPORT INSTRUCTIONS
    # =========================================================================

    dialog_title = "Landscape Generation Complete"
    
    # Format the dialog message, dynamically injecting the target heightmap path for user reference
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

    # Suspend Editor execution and visually notify the user of pending manual operations
    unreal.EditorDialog.show_message(
        dialog_title, dialog_message, unreal.AppMsgType.OK, unreal.AppReturnType.OK
    )
    return landscape_actor