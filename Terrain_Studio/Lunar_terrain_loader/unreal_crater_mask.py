"""
Unreal Ground Truth: Craters and Shadows
----------------------------------------
Executes natively within the Unreal Engine Python environment. Imports the
studio's crater map (<name>_craterMask.png, cell values = mask ids) and builds
the object-mask material so landscape pixels take the crater map's id:

    stencil == landscape id  and  crater map cell > 0   ->   that cell's id
    otherwise                                          ->   the stencil id

The same image also carries the shadow mask in three flag bits, the lowest bit
of each channel:

    green   ground (landscape, craters, rims)
    blue    in shadow: any surface the sun does not reach (never the sky)
    red     in the rover's shadow: the shadow cast by the mask camera's own actor

The colours change by at most 1/255, and the GNC stereo node splits the frame
into object_mask (flag bits cleared) and the shadow feeds. Shadows come from the
mask camera's own render: it draws every surface white, lit by the sun alone (no
bounce or sky light, no other lights), so a pixel is in shadow exactly when that
render is dark. The rover's shadow comes from a helper capture at the same pose
that renders the same view with the rover hidden, which also removes its
shadow: a pixel dark in the mask render but lit in the helper's is the rover's.

The material keeps the original colouring (a stable colour hashed from each
id), so rocks and the rover are unchanged. The crater map is read with an exact
texel fetch at the pixel's world position, using the same DEM frame as the rock
placer (columns run towards -X); it may hold several cells per DEM cell
(CraterSupersample).
"""

import builtins
import json

import unreal

from unreal_rock_placer import find_landscape, landscape_frame

CAMERAS_JSON_PREFIX = "CAMERAS_JSON "

# Assets in /Game/GroundTruth_Masks:
#   PPM_GroundTruthMask         post-process material on the mask camera: object ids,
#                               crater ids on the Landscape, shadow flag bits
#   PPM_GroundTruthMask_Backup  that material as it was before the studio first
#                               changed it (Restore Original Mask Material)
#   T_CraterIds_<map>           crater map: crater / rim id per cell
#   RT_SunOnly_RoverHidden      helper capture's sun-only render without the rover
GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
MASK_MATERIAL = f"{GROUND_TRUTH_FOLDER}/PPM_GroundTruthMask"
BACKUP_MATERIAL = f"{GROUND_TRUTH_FOLDER}/PPM_GroundTruthMask_Backup"
ROVER_SHADOW_TARGET = f"{GROUND_TRUTH_FOLDER}/RT_SunOnly_RoverHidden"
CRATER_TEXTURE_PREFIX = "T_CraterIds_"
TEXTURE_FOLDER = GROUND_TRUTH_FOLDER
# Earlier names, renamed on the next send.
OLD_ASSET_NAMES = {
    f"{GROUND_TRUTH_FOLDER}/PPM_ObjectMask": MASK_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/PPM_ObjectMask_Original": BACKUP_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/RT_SunWithoutRover": ROVER_SHADOW_TARGET,
}
ROVER_SHADOW_CAPTURE = "RoverShadowCapture"   # Helper actor label: <actor>_RoverShadowCapture
WATCH_INTERVAL = 0.5       # Seconds between the watcher's attachment checks
WATCH_RESCAN = 10.0        # Seconds between level scans while no helper is found
DEFAULT_ROVER_STENCIL = 2
LANDSCAPE_STENCIL = 10
SHADOW_THRESHOLD = 0.5     # Sun-only display value below which a pixel is shadow (half light, so soft tails count)

# Mask camera render: surfaces white, lit by the sun only, fixed exposure. The
# mask material replaces the colours, so only the mask image depends on this.
MASK_CAMERA_SHOW_FLAGS = (
    ("LightingOnlyOverride", True), ("GlobalIllumination", False),
    ("LumenGlobalIllumination", False), ("SkyLighting", False), ("AmbientOcclusion", False),
    ("DistanceFieldAO", False), ("ReflectionEnvironment", False), ("LumenReflections", False),
    ("ScreenSpaceReflections", False), ("Fog", False), ("Atmosphere", False),
    ("SpotLights", False), ("PointLights", False), ("RectLights", False),
    ("Bloom", False), ("EyeAdaptation", False))
MASK_CAMERA_EXPOSURE_BIAS = -13.0    # Sunlit white ground displays at ~0.9

# Custom node: crater lookup on landscape pixels, the original id colouring, and
# the ground and shadow flags in the lowest bits of green and blue.
MASK_CODE = r"""float v = In;                                         // Stencil value 0..255
bool ground = abs(v - LandscapeStencil) < 0.5;
if (ground)
{
    // Landscape pixel: the crater map's id at this point, if any. DEM cell c
    // spans map cells [c * S, (c + 1) * S), centred on vertex c.
    float2 dem = float2((Pos.x - CraterOriginX) / CraterStepX,
                        (Pos.y - CraterOriginY) / CraterStepY);
    int2 cell = int2(floor((dem + 0.5) * CraterSupersample));
    if (cell.x >= 0 && cell.y >= 0 && cell.x < CraterWidth && cell.y < CraterHeight)
    {
        float id = round(Crater.Load(int3(cell, 0)).r * 255.0);
        if (id > 0.5) v = id;
    }
}
// Any surface the sun does not reach: ground, rocks or rover (the mask camera
// renders sunlight only). Untagged pixels are sky.
float3 luma = float3(0.2126, 0.7152, 0.0722);
float shadow = (v >= 0.5 && ShadowEnabled > 0.5 && dot(Scene, luma) < ShadowThreshold) ? 1.0 : 0.0;
// The rover's shadow: lit in the helper render that hides the rover. Not on the
// rover itself, whose unlit side stays plain shadow.
float roverShadow = 0.0;
if (shadow > 0.5 && RoverShadowEnabled > 0.5 && abs(v - RoverStencil) >= 0.5)
{
    float w, h;
    NoRover.GetDimensions(w, h);
    int2 p = int2(min(UV * float2(w, h), float2(w, h) - 1.0));
    if (dot(NoRover.Load(int3(p, 0)).rgb, luma) >= ShadowThreshold) roverShadow = 1.0;
}
float3 c = float3(0, 0, 0);                           // Untagged pixels are black
if (v >= 0.5)
{
    c.r = frac(sin(v * 12.9898) * 43758.5453);       // Stable distinct colour per id
    c.g = frac(sin(v * 78.2330) * 43758.5453);
    c.b = frac(sin(v * 37.7190) * 43758.5453);
}
// Quantise to 8 bits; the lowest bits flag ground (g), shadow (b), rover shadow (r).
float3 c8 = round(c * 255.0);
c8.r = c8.r - fmod(c8.r, 2.0) + roverShadow;
c8.g = c8.g - fmod(c8.g, 2.0) + (ground ? 1.0 : 0.0);
c8.b = c8.b - fmod(c8.b, 2.0) + shadow;
return c8 / 255.0;"""

SCALAR_INPUTS = ("LandscapeStencil", "CraterOriginX", "CraterOriginY", "CraterStepX",
                 "CraterStepY", "CraterWidth", "CraterHeight", "CraterSupersample",
                 "ShadowEnabled", "ShadowThreshold", "RoverShadowEnabled", "RoverStencil")


def _game_landscape(label):
    """The Landscape in the running game: during Play In Editor the editor's actors are not listed."""
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()
    if world is None:
        return None
    landscapes = list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy))
    for actor in landscapes:
        if label and actor.get_actor_label() == label:
            return actor
    return landscapes[0] if len(landscapes) == 1 else None


def import_mask_texture(png_path, name):
    """Imports the crater map as an exact (uncompressed, unfiltered, no mips) texture."""
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png_path)
    task.set_editor_property("destination_path", TEXTURE_FOLDER)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("automated", True)
    task.set_editor_property("save", False)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    texture = unreal.load_asset(f"{TEXTURE_FOLDER}/{name}")
    if texture is None:
        raise RuntimeError(f"Could not import {png_path}")
    for prop, value in (
            ("compression_settings", unreal.TextureCompressionSettings.TC_GRAYSCALE),
            ("srgb", False),
            ("filter", unreal.TextureFilter.TF_NEAREST),
            ("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_NO_MIPMAPS),
            ("never_stream", True),
            ("address_x", unreal.TextureAddress.TA_CLAMP),
            ("address_y", unreal.TextureAddress.TA_CLAMP)):
        texture.set_editor_property(prop, value)
    unreal.EditorAssetLibrary.save_loaded_asset(texture)
    return texture


def _rename_old_assets():
    """Renames assets still under their earlier names (references follow). Returns notes."""
    notes = []
    for old, new in OLD_ASSET_NAMES.items():
        library = unreal.EditorAssetLibrary
        if library.does_asset_exist(old) and not library.does_asset_exist(new):
            if library.rename_asset(old, new):
                notes.append(f"renamed {old.rsplit('/', 1)[-1]} to {new.rsplit('/', 1)[-1]}")
    return notes


def _backup_material():
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        unreal.EditorAssetLibrary.duplicate_asset(MASK_MATERIAL, BACKUP_MATERIAL)


def _build_mask_graph(material, texture, values, no_rover):
    """
    Rebuilds the mask material: stencil + scene + world position + crater map
    + rover-less render (`no_rover`, a texture) -> colour.
    """
    lib = unreal.MaterialEditingLibrary
    lib.delete_all_material_expressions(material)

    stencil = lib.create_material_expression(material, unreal.MaterialExpressionSceneTexture, -900, 0)
    stencil.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_CUSTOM_STENCIL)
    red = lib.create_material_expression(material, unreal.MaterialExpressionComponentMask, -650, 0)
    for channel, keep in (("r", True), ("g", False), ("b", False), ("a", False)):
        red.set_editor_property(channel, keep)
    lib.connect_material_expressions(stencil, "Color", red, "")

    scene = lib.create_material_expression(material, unreal.MaterialExpressionSceneTexture, -900, -140)
    scene.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_POST_PROCESS_INPUT0)
    rgb = lib.create_material_expression(material, unreal.MaterialExpressionComponentMask, -650, -140)
    for channel, keep in (("r", True), ("g", True), ("b", True), ("a", False)):
        rgb.set_editor_property(channel, keep)
    lib.connect_material_expressions(scene, "Color", rgb, "")

    position = lib.create_material_expression(material, unreal.MaterialExpressionWorldPosition, -650, 120)
    crater = lib.create_material_expression(
        material, unreal.MaterialExpressionTextureObjectParameter, -650, 220)
    crater.set_editor_property("parameter_name", "CraterMask")
    crater.set_editor_property("texture", texture)
    helper = lib.create_material_expression(
        material, unreal.MaterialExpressionTextureObjectParameter, -650, -280)
    helper.set_editor_property("parameter_name", "SunWithoutRover")
    helper.set_editor_property("texture", no_rover)
    screen = lib.create_material_expression(material, unreal.MaterialExpressionScreenPosition, -650, -380)

    custom = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -300, 0)
    custom.set_editor_property("code", MASK_CODE)
    custom.set_editor_property("description", "ObjectMaskColour")
    custom.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT3)
    names = ("In", "Scene", "Pos", "Crater", "NoRover", "UV") + SCALAR_INPUTS
    inputs = []
    for name in names:
        entry = unreal.CustomInput()
        entry.set_editor_property("input_name", name)
        inputs.append(entry)
    custom.set_editor_property("inputs", inputs)

    lib.connect_material_expressions(red, "", custom, "In")
    lib.connect_material_expressions(rgb, "", custom, "Scene")
    lib.connect_material_expressions(position, "", custom, "Pos")
    lib.connect_material_expressions(crater, "", custom, "Crater")
    lib.connect_material_expressions(helper, "", custom, "NoRover")
    lib.connect_material_expressions(screen, "ViewportUV", custom, "UV")
    for row, name in enumerate(SCALAR_INPUTS):
        scalar = lib.create_material_expression(
            material, unreal.MaterialExpressionScalarParameter, -650, 360 + 70 * row)
        scalar.set_editor_property("parameter_name", name)
        scalar.set_editor_property("default_value", float(values[name]))
        lib.connect_material_expressions(scalar, "", custom, name)
    lib.connect_material_property(custom, "", unreal.MaterialProperty.MP_EMISSIVE_COLOR)

    lib.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)


def _in_pie():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).is_in_play_in_editor()


def _actors():
    """Actors of the open level, or of the running game during Play In Editor."""
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = editor.get_game_world() if _in_pie() else editor.get_editor_world()
    return list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)) if world else []


def _level_actors():
    """Actors of the open level (not the running game)."""
    return list(unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors())


def _is_helper(actor):
    return actor.get_actor_label().endswith("_" + ROVER_SHADOW_CAPTURE)


def _streamer(actor):
    for component in actor.get_components_by_class(unreal.ActorComponent):
        if component.get_class().get_name() == "StereoCamStreamer":
            return component
    return None


def _has_mask_material(capture):
    settings = capture.get_editor_property("post_process_settings")
    blends = settings.get_editor_property("weighted_blendables").get_editor_property("array")
    return any(b.get_editor_property("object") is not None
               and b.get_editor_property("object").get_path_name().startswith(MASK_MATERIAL)
               for b in blends)


def list_mask_cameras():
    """
    CAMERAS_JSON_PREFIX + JSON: every actor with scene captures, as
    {"actor", "captures", "with_mask", "streamer_mask"} (the capture its
    StereoCamStreamer streams as the object mask, or null).
    """
    found, helpers = [], False
    for actor in _actors():
        captures = actor.get_components_by_class(unreal.SceneCaptureComponent2D)
        if _is_helper(actor):
            helpers = True
        if not captures or _is_helper(actor):
            continue
        streamer = _streamer(actor)
        status = _helper_status(actor)
        if status == "detached":
            capture = next((c for c in captures if c.get_name()
                            == str(streamer.get_editor_property("mask_capture_name"))), None)
            helper = next(a for a in _actors() if a.get_actor_label()
                          == f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}")
            if capture is not None and _attach_helper(helper, capture):
                status = "reattached"
        found.append({
            "actor": actor.get_actor_label(),
            "captures": [c.get_name() for c in captures],
            "with_mask": [c.get_name() for c in captures if _has_mask_material(c)],
            "streamer_mask": str(streamer.get_editor_property("mask_capture_name")) if streamer else None,
            "rover_shadow_helper": status})
    if helpers:
        install_rover_shadow_watch()
    return CAMERAS_JSON_PREFIX + json.dumps({"pie": _in_pie(), "actors": found})


def _set_mask_render(capture):
    """Sun-only, lighting-only render with a fixed exposure (what shadow detection reads)."""
    flags = []
    for name, enabled in MASK_CAMERA_SHOW_FLAGS:
        setting = unreal.EngineShowFlagsSetting()
        setting.set_editor_property("show_flag_name", name)
        setting.set_editor_property("enabled", enabled)
        flags.append(setting)
    capture.set_editor_property("show_flag_settings", flags)
    settings = capture.get_editor_property("post_process_settings")
    for prop, value in (("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL),
                        ("auto_exposure_apply_physical_camera_exposure", False),
                        ("auto_exposure_bias", MASK_CAMERA_EXPOSURE_BIAS)):
        settings.set_editor_property("override_" + prop, True)
        settings.set_editor_property(prop, value)
    capture.set_editor_property("post_process_settings", settings)


def assign_mask_camera(actor_label, capture_name):
    """
    Makes `capture_name` on `actor_label` the object-mask camera: adds
    PPM_GroundTruthMask to its post-process materials, sets its sun-only render (for
    shadows) and, when the actor has a StereoCamStreamer, streams it as the
    mask. Returns a summary line.
    """
    actor = next((a for a in _actors() if a.get_actor_label() == actor_label), None)
    if actor is None:
        return f"[-] No actor '{actor_label}' in the level."
    capture = next((c for c in actor.get_components_by_class(unreal.SceneCaptureComponent2D)
                    if c.get_name() == capture_name), None)
    if capture is None:
        return f"[-] '{actor_label}' has no scene capture '{capture_name}'."

    notes = []
    if not _has_mask_material(capture):
        settings = capture.get_editor_property("post_process_settings")
        blendables = settings.get_editor_property("weighted_blendables")
        entries = list(blendables.get_editor_property("array"))
        entry = unreal.WeightedBlendable()
        entry.set_editor_property("weight", 1.0)
        entry.set_editor_property("object", unreal.load_asset(MASK_MATERIAL))
        entries.append(entry)
        blendables.set_editor_property("array", entries)
        settings.set_editor_property("weighted_blendables", blendables)
        capture.set_editor_property("post_process_settings", settings)
        notes.append("added PPM_GroundTruthMask")
    _set_mask_render(capture)
    notes.append("sun-only render set for shadows")
    streamer = _streamer(actor)
    if streamer is not None and str(streamer.get_editor_property("mask_capture_name")) != capture_name:
        streamer.set_editor_property("mask_capture_name", capture_name)
        notes.append(f"streamer now streams it as mask camera {streamer.get_editor_property('mask_cam_id')}")
    elif streamer is None:
        notes.append("no StereoCamStreamer on this actor, so it is not streamed")
    if _in_pie():
        notes.append("Play In Editor is running: camera changes last until it stops")
    detail = "; ".join(notes) if notes else "already set up"
    return f"Mask camera: {actor_label} / {capture_name} ({detail})."


def _find_capture(actor_label, capture_name):
    actor = next((a for a in _actors() if a.get_actor_label() == actor_label), None)
    capture = actor and next((c for c in actor.get_components_by_class(unreal.SceneCaptureComponent2D)
                              if c.get_name() == capture_name), None)
    return (actor, capture) if capture else None


def _current_mask_camera():
    """(actor, capture) of the capture a StereoCamStreamer streams as the mask, or None."""
    for actor in _actors():
        streamer = _streamer(actor)
        if streamer is not None:
            found = _find_capture(actor.get_actor_label(),
                                  str(streamer.get_editor_property("mask_capture_name")))
            if found:
                return found
    return None


def _actor_stencil(actor):
    """The CustomDepth stencil value the actor renders with (its id in the object mask)."""
    for component in actor.get_components_by_class(unreal.PrimitiveComponent):
        if component.get_editor_property("render_custom_depth"):
            return int(component.get_editor_property("custom_depth_stencil_value"))
    return DEFAULT_ROVER_STENCIL


def _rover_shadow_target(capture):
    """The helper's render target, sized like the mask camera's."""
    width, height = 640, 480
    target = capture.get_editor_property("texture_target")
    if target is not None:
        width, height = target.get_editor_property("size_x"), target.get_editor_property("size_y")
    helper = unreal.load_asset(ROVER_SHADOW_TARGET)
    if helper is None:
        helper = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            ROVER_SHADOW_TARGET.rsplit("/", 1)[-1], GROUND_TRUTH_FOLDER,
            unreal.TextureRenderTarget2D, unreal.TextureRenderTargetFactoryNew())
    helper.set_editor_property("render_target_format", unreal.TextureRenderTargetFormat.RTF_RGBA8)
    helper.set_editor_property("size_x", width)
    helper.set_editor_property("size_y", height)
    unreal.EditorAssetLibrary.save_loaded_asset(helper)
    return helper


def _remove_rover_shadow_captures():
    """Deletes the helper captures from the open level. Returns how many."""
    helpers = [a for a in _level_actors() if _is_helper(a)]
    for actor in helpers:
        actor.destroy_actor()
    return len(helpers)


HELPER_T3D = """Begin Map
   Begin Level
      Begin Actor Class=/Script/Engine.SceneCapture2D Name={name} Archetype="/Script/Engine.SceneCapture2D'/Script/Engine.Default__SceneCapture2D'"
         Begin Object Class=/Script/Engine.SceneCaptureComponent2D Name="NewSceneCaptureComponent2D" Archetype="/Script/Engine.SceneCaptureComponent2D'/Script/Engine.Default__SceneCapture2D:NewSceneCaptureComponent2D'"
         End Object
         Begin Object Class=/Script/Engine.SceneComponent Name="SceneComponent" Archetype="/Script/Engine.SceneComponent'/Script/Engine.Default__SceneCapture2D:SceneComponent'"
         End Object
         Begin Object Name="NewSceneCaptureComponent2D"
            HiddenActors(0)="{hidden}"
            AttachParent="SceneComponent"
         End Object
         CaptureComponent2D="NewSceneCaptureComponent2D"
         SceneComponent="SceneComponent"
         RootComponent="SceneComponent"
         ActorLabel="{label}"
      End Actor
   End Level
End Map
"""


def _windows_clipboard(text=None):
    """Reads the clipboard's text, or replaces it with `text`. Windows only."""
    import ctypes
    import ctypes.wintypes as wt
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.GetClipboardData.restype = wt.HANDLE
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE
    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    unicode_text = 13
    if not user32.OpenClipboard(None):
        raise RuntimeError("the clipboard is in use")
    try:
        if text is None:
            handle = user32.GetClipboardData(unicode_text)
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        data = ctypes.create_unicode_buffer(text)
        handle = kernel32.GlobalAlloc(0x0002, ctypes.sizeof(data))     # GMEM_MOVEABLE
        ctypes.memmove(kernel32.GlobalLock(handle), data, ctypes.sizeof(data))
        kernel32.GlobalUnlock(handle)
        user32.EmptyClipboard()
        user32.SetClipboardData(unicode_text, handle)
        return text
    finally:
        user32.CloseClipboard()


def _paste_helper(actor, label):
    """
    Adds a SceneCapture2D labelled `label` whose capture hides `actor`.
    HiddenActors is instance-only and Python refuses to set it in the editor
    ("cannot be edited on templates"), so the actor is pasted from T3D text with
    the list already filled in. The clipboard's text is restored afterwards.
    """
    hidden = f"{actor.get_class().get_path_name()}'{actor.get_path_name()}'"
    text = HELPER_T3D.format(name=ROVER_SHADOW_CAPTURE, hidden=hidden, label=label)
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    selected = actors.get_selected_level_actors()
    saved = _windows_clipboard()
    try:
        _windows_clipboard(text)
        unreal.SystemLibrary.execute_console_command(
            unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world(), "EDIT PASTE")
    finally:
        if saved is not None:
            _windows_clipboard(saved)
        actors.set_selected_level_actors(selected)
    return next((a for a in _level_actors() if a.get_actor_label() == label), None)


def _attach_helper(helper, capture):
    """
    Snaps the helper onto the mask camera. The camera may ride a physics body
    that moves away from the actor's root in play, so the helper must follow the
    camera itself. Recompiling the actor's Blueprint can re-attach it to the
    root; sending again fixes that. Returns True when attached to `capture`.
    """
    rule = unreal.AttachmentRule.SNAP_TO_TARGET
    helper.attach_to_component(capture, "", rule, rule, rule, False)
    return helper.get_editor_property("root_component").get_attach_parent() == capture


def _helper_status(actor):
    """'attached', 'detached' or None: the actor's rover-shadow helper and its mask camera."""
    helper = next((a for a in _actors() if a.get_actor_label()
                   == f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"), None)
    streamer = _streamer(actor)
    if helper is None or streamer is None:
        return None
    parent = helper.get_editor_property("root_component").get_attach_parent()
    name = str(streamer.get_editor_property("mask_capture_name"))
    return "attached" if parent is not None and parent.get_name() == name else "detached"


def _helper_pairs(world):
    """
    (helper, mask capture) for every rover-shadow helper in `world`. Looks only
    at SceneCapture2D actors (a few), not the whole level, which can hold
    thousands of rocks; the helper's owner is its attach-parent actor (it stays
    the rover even when re-attached to the root).
    """
    pairs, everything = [], None
    for helper in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.SceneCapture2D):
        label = helper.get_actor_label()
        if not label.endswith("_" + ROVER_SHADOW_CAPTURE):
            continue
        owner_label = label[:-len(ROVER_SHADOW_CAPTURE) - 1]
        owner = helper.get_attach_parent_actor()
        if owner is None or owner.get_actor_label() != owner_label:
            if everything is None:      # Fully detached: fall back to a full scan
                everything = {a.get_actor_label(): a for a in
                              unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)}
            owner = everything.get(owner_label)
        streamer = _streamer(owner) if owner else None
        if streamer is None:
            continue
        name = str(streamer.get_editor_property("mask_capture_name"))
        capture = next((c for c in owner.get_components_by_class(unreal.SceneCaptureComponent2D)
                        if c.get_name() == name), None)
        if capture is not None:
            pairs.append((helper, capture))
    return pairs


def _watch_tick(delta_seconds):
    """
    Editor tick: keeps each rover-shadow helper attached to its mask camera, in
    the level and in a running game. Blueprint recompiles (also the one that can
    run when Play starts) re-attach it to the actor's root.
    """
    state = getattr(builtins, "_rover_shadow_watch", None)
    if state is None:
        return
    state["elapsed"] += delta_seconds
    state["since_scan"] += delta_seconds
    if state["elapsed"] < WATCH_INTERVAL:
        return
    state["elapsed"] = 0.0
    try:
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = editor.get_game_world() or editor.get_editor_world()
        if world is None:
            return
        valid = unreal.SystemLibrary.is_valid
        stale = (state["world"] != world.get_path_name()
                 or not all(valid(h) and valid(c) for h, c in state["pairs"])
                 or (not state["pairs"] and state["since_scan"] >= WATCH_RESCAN))
        if stale:
            state["pairs"] = _helper_pairs(world)
            state["world"] = world.get_path_name()
            state["since_scan"] = 0.0
        for helper, capture in state["pairs"]:
            if helper.get_editor_property("root_component").get_attach_parent() != capture:
                attached = _attach_helper(helper, capture)
                unreal.log(f"Rover shadow: re-attached {helper.get_actor_label()} to "
                           f"{capture.get_name()}" + ("" if attached else " (failed)"))
        state["failed"] = False
    except Exception as error:      # Never let the tick raise; report once
        if not state.get("failed"):
            unreal.log_warning(f"Rover shadow watcher: {error}")
        state["failed"] = True


def install_rover_shadow_watch():
    """
    Registers the attachment watcher for this editor session (replacing an
    earlier one). It lasts until the editor closes; sending masks or Find in the
    studio installs it again.
    """
    uninstall_rover_shadow_watch()
    state = {"elapsed": 0.0, "since_scan": 0.0, "world": None, "pairs": [], "failed": False}
    state["handle"] = unreal.register_slate_post_tick_callback(_watch_tick)
    builtins._rover_shadow_watch = state


def uninstall_rover_shadow_watch():
    state = getattr(builtins, "_rover_shadow_watch", None)
    if state is not None:
        unreal.unregister_slate_post_tick_callback(state["handle"])
        builtins._rover_shadow_watch = None


def _hides(helper, actor):
    component = helper.get_component_by_class(unreal.SceneCaptureComponent2D)
    return any(a == actor for a in component.get_editor_property("hidden_actors"))


def setup_rover_shadow(actor, capture, target):
    """
    Adds (or updates) the helper capture for the rover's shadow: attached to the
    mask camera, same view, sun-only render into `target`, with `actor` hidden,
    which also hides its shadow. Rendered before the mask camera each frame.
    Returns (set up, summary line).
    """
    label = f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"
    helper = next((a for a in _actors() if a.get_actor_label() == label), None)
    if helper is None or not _hides(helper, actor):
        if _in_pie():
            return False, ("Rover shadow: off; stop Play In Editor and send again, its helper "
                           "capture can only be added to the level in the editor.")
        if helper is not None:
            helper.destroy_actor()
        try:
            helper = _paste_helper(actor, label)
        except Exception as error:      # Clipboard busy, not Windows, ...
            helper, reason = None, str(error)
        else:
            reason = "the editor did not paste it"
        if helper is None or not _hides(helper, actor):
            return False, f"Rover shadow: off, could not add its helper capture ({reason})."
    _attach_helper(helper, capture)
    component = helper.get_component_by_class(unreal.SceneCaptureComponent2D)
    component.set_editor_property("fov_angle", capture.get_editor_property("fov_angle"))
    component.set_editor_property("texture_target", target)
    component.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
    component.set_editor_property("capture_every_frame", True)
    component.set_editor_property("capture_sort_priority",
                                  capture.get_editor_property("capture_sort_priority") + 1)
    _set_mask_render(component)
    return True, (f"Rover shadow: on, {label} renders {actor.get_actor_label()}'s view "
                  f"without it into {ROVER_SHADOW_TARGET} (flag bit in red).")


def apply_crater_mask(png_path, width, height, resolution=1.0, landscape_label="",
                      landscape_width=0, landscape_stencil=LANDSCAPE_STENCIL, supersample=1,
                      camera_actor="", camera_capture="", save_level=False, shadows=True,
                      rover_shadow=True):
    """
    Imports the crater map and makes the object mask show it on the Landscape;
    with `shadows`, the frame also carries the shadow mask and, with
    `rover_shadow`, flags the shadow of the mask camera's own actor. When given,
    sets up `camera_capture` on `camera_actor` as the mask camera. Returns a
    summary.
    """
    landscape = find_landscape(landscape_label) or _game_landscape(landscape_label)
    if landscape is None:
        return "[-] No Landscape found to align the crater map with."
    x0, y0, step_x, step_y = landscape_frame(landscape, landscape_width, resolution)

    renamed = _rename_old_assets()
    stem = png_path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem.endswith("_craterMask"):
        stem = stem[:-len("_craterMask")]
    texture = import_mask_texture(png_path, CRATER_TEXTURE_PREFIX + stem)
    material = unreal.load_asset(MASK_MATERIAL)
    if material is None:
        return f"[-] {MASK_MATERIAL} not found: set up the object mask first (StereoCamStreamer README)."
    _backup_material()

    # Rover shadow: helper capture on the mask camera (the one given, or the
    # one currently streamed).
    rover_note, no_rover, rover_stencil, rover_on = "", texture, DEFAULT_ROVER_STENCIL, False
    if shadows and rover_shadow:
        found = (_find_capture(camera_actor, camera_capture) if camera_actor and camera_capture
                 else _current_mask_camera())
        if found is None:
            rover_note = "Rover shadow: off, no mask camera found (choose one and send again)."
        else:
            actor, capture = found
            target = _rover_shadow_target(capture)
            rover_on, rover_note = setup_rover_shadow(actor, capture, target)
            if rover_on:
                no_rover, rover_stencil = target, _actor_stencil(actor)
    elif not _in_pie() and _remove_rover_shadow_captures():
        rover_note = "Rover shadow: off (helper capture removed)."

    _build_mask_graph(material, texture, {
        "LandscapeStencil": landscape_stencil, "CraterOriginX": x0, "CraterOriginY": y0,
        "CraterStepX": step_x, "CraterStepY": step_y,
        "CraterWidth": width, "CraterHeight": height, "CraterSupersample": supersample,
        "ShadowEnabled": 1.0 if shadows else 0.0, "ShadowThreshold": SHADOW_THRESHOLD,
        "RoverShadowEnabled": 1.0 if rover_on else 0.0, "RoverStencil": rover_stencil},
        no_rover)
    summary = (f"Crater ground truth on '{landscape.get_actor_label()}': {texture.get_path_name()} "
               f"({width} x {height}, {supersample} per DEM cell) in {MASK_MATERIAL}. "
               f"Backup: {BACKUP_MATERIAL}.")
    summary += ("\nShadow mask: on (flag bits in green and blue; split into its own feed by GNC)."
                if shadows else "\nShadow mask: off.")
    if rover_note:
        summary += "\n" + rover_note
    if renamed:
        summary += "\nAssets: " + "; ".join(renamed) + "."
    if camera_actor and camera_capture:
        summary += "\n" + assign_mask_camera(camera_actor, camera_capture)
    if rover_on:
        helper = next((a for a in _actors() if a.get_actor_label()
                       == f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"), None)
        if helper is None or not _attach_helper(helper, capture):
            summary += ("\n[-] Rover shadow: the helper capture is not attached to the mask "
                        "camera; send again.")
        install_rover_shadow_watch()
        summary += ("\nRover shadow: a watcher keeps the helper attached to the mask camera "
                    "until the editor closes.")
    elif not _in_pie():
        uninstall_rover_shadow_watch()
    if save_level and not _in_pie():
        import unreal_level_saver
        summary += "\n" + unreal_level_saver.save_current_level()
    return summary


def remove_crater_mask():
    """
    Restores the object-mask material saved before the crater map was added and
    removes the rover-shadow helper captures.
    """
    removed = 0 if _in_pie() else _remove_rover_shadow_captures()
    if removed:
        uninstall_rover_shadow_watch()
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        return ("Nothing to restore: no backup of the object-mask material."
                + (f" Removed {removed} rover-shadow helper capture(s)." if removed else ""))
    unreal.EditorAssetLibrary.delete_asset(MASK_MATERIAL)
    unreal.EditorAssetLibrary.rename_asset(BACKUP_MATERIAL, MASK_MATERIAL)
    return (f"Restored the original {MASK_MATERIAL}."
            + (f" Removed {removed} rover-shadow helper capture(s)." if removed else ""))
