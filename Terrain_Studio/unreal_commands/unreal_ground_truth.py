"""
Unreal Ground Truth: Craters and Shadows
----------------------------------------
Executes natively within the Unreal Engine Python environment. Imports the
studio's crater map (<name>_craterMask.png, cell values = mask ids) and builds
the object-mask material so landscape pixels take the crater map's id:

    stencil == landscape id  and  crater map cell > 0   ->   that cell's id
    otherwise                                          ->   the stencil id
    no stencil and nothing within SKY_DEPTH_CM          ->   SKY_ID

Object-mask ids are consecutive (MASK_IDS), each drawn in a fixed colour
(MASK_PALETTE; the GNC dashboard uses the same table):

    0 unlabeled   1 sky   2 rover   3 sun   4 sun_glare   5 landscape
    6 crater   7 crater_rim   8 wheel_track   9 boulder   10 small_rock

The rover, the landscape and the rocks carry their id as their CustomDepth
stencil; the others are worked out here. Ids above 10 (custom stencils) get a
colour hashed from the id.

The same image also carries the shadow mask in three flag bits, the lowest bit
of each channel:

    green   ground (landscape, craters, rims)
    blue    in shadow: any surface the sun does not reach (never the sky)
    red     in the rover's shadow: the shadow cast by the mask camera's own actor
            (with blue); without blue, the sky (sky, sun, glare)

The sun's disc gets its own id in the object mask (SUN_ID): sky pixels within
the sun's angular radius of its direction. A ring around it out to a glare radius
gets SUN_GLARE_ID: the sky the navcam washes out with bloom around the sun. That direction and radius come from
the level's Directional Light through MPC_Sun, which the editor watcher keeps
current (also in a running game). The shadow feeds treat the disc as sky.

Wheel tracks (unreal_track_setup, laid by the watcher's unreal_wheel_tracks) are decals that write a small metallic value
regolith never has; ground pixels carrying it get TRACK_ID.

The colours change by at most 1/255, and the GNC stereo node splits the frame
into object_mask (flag bits cleared) and the shadow feeds. Shadows come from the
mask camera's own render: it draws every surface white, lit by the sun alone (no
bounce or sky light, no other lights), so a pixel is in shadow exactly when that
render is dark. The rover's shadow comes from the rover's own shadow map: a
helper capture renders only the rover's depth, looking along the sunlight
(orthographic, centred on the mask camera, moved every frame by the editor
watcher). A shadowed pixel is the rover's shadow when the rover lies between it
and the sun, even where a rock, crater wall or slope shades it too.

The crater map is read with an exact
texel fetch at the pixel's world position, using the same DEM frame as the rock
placer (columns run towards -X); it may hold several cells per DEM cell
(CraterSupersample).
"""

import json
import math

import unreal

from unreal_rocks import find_landscape, landscape_frame
from unreal_watcher import (ROVER_SHADOW_CAPTURE, SHADOW_MAP_PARAMETERS, SUN_MPC, SUN_PARAMETER,
                            find_streamer, install_watcher, place_shadow_map, sun_light,
                            sun_vector, uninstall_watcher, update_sun_mask)

CAMERAS_JSON_PREFIX = "CAMERAS_JSON "

# Assets in /Game/GroundTruth_Masks:
#   Mask/PPM_GroundTruthMask         post-process material on the mask camera: object
#                                    ids, crater ids on the Landscape, shadow flag bits
#   Mask/PPM_GroundTruthMask_Backup  that material as it was before the studio first
#                                    changed it (Restore Original Mask Material)
#   CraterMaps/T_CraterIds_<map>     crater map: crater / rim id per cell
#   RoverShadow/RT_RoverDepthFromSun  the rover's shadow map: its depth seen from the sun
#   Sun/MPC_Sun                      sun direction and disc size for the sun's id
#   WheelTracks/M_WheelTrackDecal    wheel-track decal material (unreal_track_setup)
GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
MASK_MATERIAL = f"{GROUND_TRUTH_FOLDER}/Mask/PPM_GroundTruthMask"
BACKUP_MATERIAL = f"{GROUND_TRUTH_FOLDER}/Mask/PPM_GroundTruthMask_Backup"
ROVER_SHADOW_TARGET = f"{GROUND_TRUTH_FOLDER}/RoverShadow/RT_RoverDepthFromSun"
SHADOW_MAP_SIZE = 512          # Texels across the rover's shadow map
SHADOW_MAP_MARGIN = 1.25       # Its width, over the rover's size seen from any side
ROVER_SHADOW_BIAS_CM = 3.0     # A point is in the rover's shadow if the rover is this much nearer the sun
CRATER_TEXTURE_PREFIX = "T_CraterIds_"
# Object-mask ids: consecutive, 0 = nothing tagged. Must match mask_labels in
# component_stereo_depth/config/params.yaml and MASK_COLOURS in the GNC
# dashboard (feed_legends.js).
MASK_IDS = {0: "unlabeled", 1: "sky", 2: "rover", 3: "sun", 4: "sun_glare", 5: "landscape",
            6: "crater", 7: "crater_rim", 8: "wheel_track", 9: "boulder", 10: "small_rock"}
# Colour per id (8-bit, even values: the lowest bit of each channel carries the
# shadow flags). The earlier hashed colours of each class, kept; the sky is new.
MASK_PALETTE = {0: (0, 0, 0), 1: (36, 40, 96), 2: (250, 194, 16), 3: (28, 128, 162),
                4: (230, 14, 118), 5: (10, 96, 216), 6: (124, 50, 208), 7: (86, 52, 82),
                8: (64, 124, 162), 9: (238, 244, 242), 10: (210, 62, 6)}
SKY_ID = 1
SUN_ID = 3
SUN_GLARE_ID = 4
SUN_GLARE_DEG = 1.1    # Glare ring radius: the navcam's saturated core (~8 px of 640 at 90 deg)
TRACK_ID = 8
SKY_DEPTH_CM = 1.0e7   # Untagged pixels farther than this (100 km) are sky
TEXTURE_FOLDER = f"{GROUND_TRUTH_FOLDER}/CraterMaps"
# Earlier names and places (all in the folder itself), moved on the next send;
# crater maps (T_CraterIds_*) left there move to TEXTURE_FOLDER.
OLD_ASSET_NAMES = {
    f"{GROUND_TRUTH_FOLDER}/PPM_ObjectMask": MASK_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/PPM_ObjectMask_Original": BACKUP_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/RT_SunWithoutRover": ROVER_SHADOW_TARGET,
    f"{GROUND_TRUTH_FOLDER}/PPM_GroundTruthMask": MASK_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/PPM_GroundTruthMask_Backup": BACKUP_MATERIAL,
    f"{GROUND_TRUTH_FOLDER}/RT_SunOnly_RoverHidden": ROVER_SHADOW_TARGET,
    f"{GROUND_TRUTH_FOLDER}/RoverShadow/RT_SunOnly_RoverHidden": ROVER_SHADOW_TARGET,
    f"{GROUND_TRUTH_FOLDER}/MPC_Sun": SUN_MPC,
    f"{GROUND_TRUTH_FOLDER}/M_WheelTrackDecal": f"{GROUND_TRUTH_FOLDER}/WheelTracks/M_WheelTrackDecal",
}
DEFAULT_ROVER_STENCIL = 2     # MASK_IDS rover (set in the rover's Blueprint)
LANDSCAPE_STENCIL = 5         # MASK_IDS landscape (unreal_landscape sets it)
# Stencils of an earlier numbering, re-tagged on the next send.
OLD_LANDSCAPE_STENCILS = (10,)
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
    // Wheel track: its decal leaves a metallic marker regolith never has.
    if (TrackEnabled > 0.5 && Metal >= TrackMetallic * 0.5) v = TrackId;
}
// Any surface the sun does not reach: ground, rocks or rover (the mask camera
// renders sunlight only). Untagged pixels are sky or unlabeled objects.
float3 luma = float3(0.2126, 0.7152, 0.0722);
float shadow = (v >= 0.5 && ShadowEnabled > 0.5 && dot(Scene, luma) < ShadowThreshold) ? 1.0 : 0.0;
// The rover's shadow: the rover lies between this point and the sun, by its
// shadow map (its depth seen along the sunlight, MapOrigin/MapRight/MapUp), so
// it wins over any other shadow on the same point. Not on the rover itself,
// whose unlit side stays plain shadow.
float roverShadow = 0.0;
if (shadow > 0.5 && RoverShadowEnabled > 0.5 && abs(v - RoverStencil) >= 0.5 && MapOrigin.w > 0.0)
{
    float3 rel = Pos - MapOrigin.xyz;
    float2 uv = float2(dot(rel, MapRight.xyz), -dot(rel, MapUp.xyz)) / (2.0 * MapOrigin.w) + 0.5;
    if (uv.x >= 0.0 && uv.y >= 0.0 && uv.x < 1.0 && uv.y < 1.0)
    {
        float w, h;
        RoverDepth.GetDimensions(w, h);
        float rover = RoverDepth.Load(int3(int2(uv * float2(w, h)), 0)).r;
        float here = dot(rel, -SunDir.xyz);           // Distance along the sunlight
        if (rover < here - RoverShadowBias) roverShadow = 1.0;
    }
}
// The sky: untagged pixels with nothing nearer than SkyDepth. Within the sun's
// angular radius it is the sun's disc, then its glare ring. Flagged with red but
// not blue, so the shadow feeds keep all of it as sky.
float sky = 0.0;
if (v < 0.5 && Depth > SkyDepth)
{
    v = SkyId;
    sky = 1.0;
    if (SunEnabled > 0.5)
    {
        float toSun = dot(-CamVec, SunDir.xyz);
        if (toSun >= SunDir.w) v = SunId;
        else if (toSun >= SunGlareCos) v = SunGlareId;
    }
}
// Colour: the fixed palette for ids 0-10 (MASK_PALETTE), a hashed colour above.
float3 c8 = float3(0, 0, 0);                          // 0: unlabeled, black
int id = (int)round(v);
PALETTE_CODE
else if (id > 0)
{
    c8 = round(float3(frac(sin(v * 12.9898) * 43758.5453), frac(sin(v * 78.2330) * 43758.5453),
                      frac(sin(v * 37.7190) * 43758.5453)) * 255.0);
}
// The lowest bits flag ground (g), shadow (b), rover shadow (r with b) and the
// sky (r without b).
c8.r = c8.r - fmod(c8.r, 2.0) + max(roverShadow, sky);
c8.g = c8.g - fmod(c8.g, 2.0) + (ground ? 1.0 : 0.0);
c8.b = c8.b - fmod(c8.b, 2.0) + shadow;
return c8 / 255.0;"""

MASK_CODE = MASK_CODE.replace("PALETTE_CODE", "\n".join(
    f"{'if' if i == 1 else 'else if'} (id == {i}) c8 = float3{rgb};"
    for i, rgb in sorted(MASK_PALETTE.items()) if i > 0))

SCALAR_INPUTS = ("LandscapeStencil", "CraterOriginX", "CraterOriginY", "CraterStepX",
                 "CraterStepY", "CraterWidth", "CraterHeight", "CraterSupersample",
                 "ShadowEnabled", "ShadowThreshold", "RoverShadowEnabled", "RoverStencil",
                 "SunEnabled", "SunId", "SunGlareId", "SunGlareCos",
                 "TrackEnabled", "TrackId", "TrackMetallic", "SkyId", "SkyDepth",
                 "RoverShadowBias")


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


def _sun_collection(world):
    """MPC_Sun (created if needed) with its default set to the level's sun now."""
    collection = unreal.load_asset(SUN_MPC)
    if collection is None:
        collection = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            SUN_MPC.rsplit("/", 1)[-1], SUN_MPC.rsplit("/", 1)[0], unreal.MaterialParameterCollection,
            unreal.MaterialParameterCollectionFactoryNew())
    light = sun_light(world)
    parameters = []
    defaults = [(SUN_PARAMETER, sun_vector(light) if light else unreal.LinearColor(0.0, 0.0, 1.0, 1.0))]
    defaults += [(name, unreal.LinearColor(0.0, 0.0, 0.0, 0.0)) for name in SHADOW_MAP_PARAMETERS]
    for name, value in defaults:               # The shadow map's are set by the watcher
        parameter = unreal.CollectionVectorParameter()
        parameter.set_editor_property("parameter_name", name)
        parameter.set_editor_property("default_value", value)
        parameters.append(parameter)
    collection.set_editor_property("vector_parameters", parameters)
    unreal.EditorAssetLibrary.save_loaded_asset(collection)
    return collection


def _rename_old_assets():
    """Renames assets still under their earlier names (references follow). Returns notes."""
    notes = []
    library = unreal.EditorAssetLibrary
    moves = dict(OLD_ASSET_NAMES)
    for path in library.list_assets(GROUND_TRUTH_FOLDER, recursive=False):
        name = path.split(".")[0].rsplit("/", 1)[-1]
        if name.startswith(CRATER_TEXTURE_PREFIX):
            moves[f"{GROUND_TRUTH_FOLDER}/{name}"] = f"{TEXTURE_FOLDER}/{name}"
    for old, new in moves.items():
        if library.does_asset_exist(old) and not library.does_asset_exist(new):
            if library.rename_asset(old, new):
                notes.append(f"moved {old[len(GROUND_TRUTH_FOLDER) + 1:]} to "
                             f"{new[len(GROUND_TRUTH_FOLDER) + 1:]}")
    return notes


def _retag_landscape(landscape, stencil):
    """
    Gives the Landscape its mask stencil if it still has an earlier one (editor
    only: never on a running game). Returns a note, or "" when nothing changed.
    """
    if _in_pie():
        return ""
    current = int(landscape.get_editor_property("custom_depth_stencil_value"))
    if current == int(stencil):
        return ""
    landscape.set_editor_property("render_custom_depth", True)
    landscape.set_editor_property("custom_depth_stencil_value", int(stencil))
    return f"Landscape: mask stencil {current} -> {int(stencil)} (consecutive mask ids)."


def _backup_material():
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        unreal.EditorAssetLibrary.duplicate_asset(MASK_MATERIAL, BACKUP_MATERIAL)


def _build_mask_graph(material, texture, values, no_rover, sun_collection):
    """
    Rebuilds the mask material: stencil + scene + world position + crater map
    + the rover's shadow map (`no_rover`, a texture, and its frame in MPC_Sun)
    + sun direction (MPC_Sun) -> colour.
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
    helper.set_editor_property("parameter_name", "RoverDepthFromSun")
    helper.set_editor_property("texture", no_rover)
    frames = []
    for row, name in enumerate(SHADOW_MAP_PARAMETERS):
        node = lib.create_material_expression(material, unreal.MaterialExpressionCollectionParameter,
                                              -650, -820 - 80 * row)
        node.set_editor_property("collection", sun_collection)
        node.set_editor_property("parameter_name", name)
        frames.append(node)
    camera = lib.create_material_expression(material, unreal.MaterialExpressionCameraVectorWS, -650, -460)
    metallic = lib.create_material_expression(material, unreal.MaterialExpressionSceneTexture, -900, -620)
    metallic.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_METALLIC)
    metal = lib.create_material_expression(material, unreal.MaterialExpressionComponentMask, -650, -620)
    for channel, keep in (("r", True), ("g", False), ("b", False), ("a", False)):
        metal.set_editor_property(channel, keep)
    lib.connect_material_expressions(metallic, "Color", metal, "")
    depth = lib.create_material_expression(material, unreal.MaterialExpressionSceneTexture, -900, -720)
    depth.set_editor_property("scene_texture_id", unreal.SceneTextureId.PPI_SCENE_DEPTH)
    depth_r = lib.create_material_expression(material, unreal.MaterialExpressionComponentMask, -650, -720)
    for channel, keep in (("r", True), ("g", False), ("b", False), ("a", False)):
        depth_r.set_editor_property(channel, keep)
    lib.connect_material_expressions(depth, "Color", depth_r, "")
    sun = lib.create_material_expression(material, unreal.MaterialExpressionCollectionParameter, -650, -540)
    sun.set_editor_property("collection", sun_collection)
    sun.set_editor_property("parameter_name", SUN_PARAMETER)

    custom = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -300, 0)
    custom.set_editor_property("code", MASK_CODE)
    custom.set_editor_property("description", "ObjectMaskColour")
    custom.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT3)
    names = ("In", "Scene", "Pos", "Crater", "RoverDepth", "MapOrigin", "MapRight", "MapUp",
             "CamVec", "SunDir", "Metal", "Depth") + SCALAR_INPUTS
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
    lib.connect_material_expressions(helper, "", custom, "RoverDepth")
    for node, name in zip(frames, ("MapOrigin", "MapRight", "MapUp")):
        lib.connect_material_expressions(node, "", custom, name)
    lib.connect_material_expressions(camera, "", custom, "CamVec")
    lib.connect_material_expressions(sun, "", custom, "SunDir")
    lib.connect_material_expressions(metal, "", custom, "Metal")
    lib.connect_material_expressions(depth_r, "", custom, "Depth")
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


def _current_world():
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return editor.get_game_world() or editor.get_editor_world()


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
    UnrealStereoCam streams as the object mask, or null).
    """
    found, helpers = [], False
    for actor in _actors():
        captures = actor.get_components_by_class(unreal.SceneCaptureComponent2D)
        if _is_helper(actor):
            helpers = True
        if not captures or _is_helper(actor):
            continue
        streamer = find_streamer(actor)
        status = _helper_status(actor)
        found.append({
            "actor": actor.get_actor_label(),
            "captures": [c.get_name() for c in captures],
            "with_mask": [c.get_name() for c in captures if _has_mask_material(c)],
            "streamer_mask": str(streamer.get_editor_property("mask_capture_name")) if streamer else None,
            "rover_shadow_helper": status})
    if helpers or unreal.EditorAssetLibrary.does_asset_exist(SUN_MPC):
        install_watcher()
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
    shadows) and, when the actor has an UnrealStereoCam, streams it as the
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
    streamer = find_streamer(actor)
    if streamer is not None and str(streamer.get_editor_property("mask_capture_name")) != capture_name:
        streamer.set_editor_property("mask_capture_name", capture_name)
        notes.append(f"streamer now streams it as mask camera {streamer.get_editor_property('mask_cam_id')}")
    elif streamer is None:
        notes.append("no UnrealStereoCam on this actor, so it is not streamed")
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
    """(actor, capture) of the capture an UnrealStereoCam streams as the mask, or None."""
    for actor in _actors():
        streamer = find_streamer(actor)
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
    """The rover's shadow map: SHADOW_MAP_SIZE square, one float (depth, cm) per texel."""
    width = height = SHADOW_MAP_SIZE
    helper = unreal.load_asset(ROVER_SHADOW_TARGET)
    if helper is None:
        helper = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            ROVER_SHADOW_TARGET.rsplit("/", 1)[-1], ROVER_SHADOW_TARGET.rsplit("/", 1)[0],
            unreal.TextureRenderTarget2D, unreal.TextureRenderTargetFactoryNew())
    helper.set_editor_property("render_target_format", unreal.TextureRenderTargetFormat.RTF_R32F)
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
            PrimitiveRenderMode=PRM_UseShowOnlyList
            ShowOnlyActors(0)="{shown}"
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
    Adds a SceneCapture2D labelled `label` whose capture shows only `actor`.
    ShowOnlyActors is instance-only and Python refuses to set it in the editor
    ("cannot be edited on templates"), so the actor is pasted from T3D text with
    the list already filled in. The clipboard's text is restored afterwards.
    """
    shown = f"{actor.get_class().get_path_name()}'{actor.get_path_name()}'"
    text = HELPER_T3D.format(name=ROVER_SHADOW_CAPTURE, shown=shown, label=label)
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


def _helper_status(actor):
    """'on' if the actor has its rover shadow-map capture, else None."""
    helper = next((a for a in _actors() if a.get_actor_label()
                   == f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"), None)
    return "on" if helper is not None and _shows(helper, actor) else None


def _shows(helper, actor):
    """True if the helper captures only `actor` (the shadow-map version of the helper)."""
    component = helper.get_component_by_class(unreal.SceneCaptureComponent2D)
    return (component.get_editor_property("primitive_render_mode")
            == unreal.SceneCapturePrimitiveRenderMode.PRM_USE_SHOW_ONLY_LIST
            and any(a == actor for a in component.get_editor_property("show_only_actors")))


def setup_rover_shadow(actor, capture, target):
    """
    Adds (or updates) the rover's shadow map: a capture of `actor` alone, its
    depth seen along the sunlight (orthographic, wide enough for the rover seen
    from any side and centred on the mask camera `capture`) into `target`,
    rendered before the mask camera each frame. The editor watcher keeps it
    pointed at the rover. Returns (set up, summary line).
    """
    label = f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"
    helper = next((a for a in _actors() if a.get_actor_label() == label), None)
    if helper is None or not _shows(helper, actor):
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
        if helper is None or not _shows(helper, actor):
            return False, f"Rover shadow: off, could not add its helper capture ({reason})."
    # Wide enough for the whole rover seen from any side, around the mask camera:
    # its visible meshes only (the actor's bounds include editor gizmos such as
    # the cameras' 10 m frustums).
    camera = capture.get_world_location()
    reach = 50.0
    for component in actor.get_components_by_class(unreal.MeshComponent):
        if component.is_visible():
            origin, extent, _radius = unreal.SystemLibrary.get_component_bounds(component)
            reach = max(reach, (origin - camera).length() + extent.length())
    width = 2.0 * SHADOW_MAP_MARGIN * reach
    component = helper.get_component_by_class(unreal.SceneCaptureComponent2D)
    component.set_editor_property("projection_type", unreal.CameraProjectionMode.ORTHOGRAPHIC)
    component.set_editor_property("ortho_width", width)
    component.set_editor_property("texture_target", target)
    component.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_SCENE_DEPTH)
    component.set_editor_property("capture_every_frame", True)
    component.set_editor_property("capture_sort_priority",
                                  capture.get_editor_property("capture_sort_priority") + 1)
    place_shadow_map(_current_world(), helper, capture, {})
    return True, (f"Rover shadow: on, {label} is {actor.get_actor_label()}'s shadow map "
                  f"({width / 100:.1f} m wide, {SHADOW_MAP_SIZE}^2 into {ROVER_SHADOW_TARGET}); "
                  f"the rover's shadow wins over other shadows (flag bit in red).")


def apply_crater_mask(png_path, width, height, resolution=1.0, landscape_label="",
                      landscape_width=0, landscape_stencil=LANDSCAPE_STENCIL, supersample=1,
                      camera_actor="", camera_capture="", save_level=False, shadows=True,
                      rover_shadow=True, sun=True, sun_glare_deg=SUN_GLARE_DEG,
                      wheel_tracks=True, track_style=None):
    """
    Imports the crater map and makes the object mask show it on the Landscape;
    with `shadows`, the frame also carries the shadow mask and, with
    `rover_shadow`, flags the shadow of the mask camera's own actor; with `sun`,
    the sun's disc gets id SUN_ID in the object mask and the sky around it out to
    `sun_glare_deg` gets SUN_GLARE_ID (0 for none); with `wheel_tracks`, the mask
    camera's actor leaves wheel tracks (TRACK_ID) during Play, with the tread look
    `track_style` (unreal_track_setup.DEFAULT_STYLE). When given,
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
        return f"[-] {MASK_MATERIAL} not found: set up the object mask first (UnrealStereoCam README)."
    _backup_material()
    retagged = _retag_landscape(landscape, landscape_stencil)

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

    # Wheel tracks: decal pool for the mask camera's actor (editor only).
    import importlib
    import unreal_track_setup
    importlib.reload(unreal_track_setup)
    track_note, tracks_on = "", False
    if wheel_tracks:
        found = (_find_capture(camera_actor, camera_capture) if camera_actor and camera_capture
                 else _current_mask_camera())
        if _in_pie():
            tracks_on = bool(unreal_track_setup.track_pool(_current_world()))
            track_note = (unreal_track_setup.apply_track_style(track_style) if tracks_on
                          else "Wheel tracks: stop Play In Editor and send again to place their "
                               "decals.")
        elif found is None:
            track_note = "Wheel tracks: off, no mask camera found (choose one and send again)."
        else:
            tracks_on, track_note = unreal_track_setup.setup_wheel_tracks(found[0],
                                                                           style=track_style)
    elif not _in_pie() and unreal_track_setup.remove_wheel_tracks():
        track_note = "Wheel tracks: off (decals removed)."

    _build_mask_graph(material, texture, {
        "LandscapeStencil": landscape_stencil, "CraterOriginX": x0, "CraterOriginY": y0,
        "CraterStepX": step_x, "CraterStepY": step_y,
        "CraterWidth": width, "CraterHeight": height, "CraterSupersample": supersample,
        "ShadowEnabled": 1.0 if shadows else 0.0, "ShadowThreshold": SHADOW_THRESHOLD,
        "RoverShadowEnabled": 1.0 if rover_on else 0.0, "RoverStencil": rover_stencil,
        "SunEnabled": 1.0 if sun else 0.0, "SunId": SUN_ID, "SunGlareId": SUN_GLARE_ID,
        "SunGlareCos": math.cos(math.radians(max(float(sun_glare_deg), 0.0))),
        "TrackEnabled": 1.0 if tracks_on else 0.0, "TrackId": TRACK_ID,
        "SkyId": SKY_ID, "SkyDepth": SKY_DEPTH_CM, "RoverShadowBias": ROVER_SHADOW_BIAS_CM,
        "TrackMetallic": unreal_track_setup.TRACK_METALLIC},
        no_rover, _sun_collection(_current_world()))
    summary = (f"Crater ground truth on '{landscape.get_actor_label()}': {texture.get_path_name()} "
               f"({width} x {height}, {supersample} per DEM cell) in {MASK_MATERIAL}. "
               f"Backup: {BACKUP_MATERIAL}.")
    if retagged:
        summary += "\n" + retagged
    summary += ("\nShadow mask: on (flag bits in green and blue; split into its own feed by GNC)."
                if shadows else "\nShadow mask: off.")
    if rover_note:
        summary += "\n" + rover_note
    summary += (f"\nSky: id {SKY_ID}; the sun's disc id {SUN_ID} and its glare (to "
                f"{sun_glare_deg:g} deg) id {SUN_GLARE_ID} in the object mask (sky in the "
                f"shadow feeds)."
                if sun else "\nSun: not tagged.")
    if track_note:
        summary += "\n" + track_note + (f" Track pixels are id {TRACK_ID} in the object mask."
                                        if tracks_on else "")
    if renamed:
        summary += "\nAssets: " + "; ".join(renamed) + "."
    if camera_actor and camera_capture:
        summary += "\n" + assign_mask_camera(camera_actor, camera_capture)
    if sun:
        update_sun_mask(_current_world())
    if tracks_on and not rover_on:
        install_watcher()
    if rover_on:
        helper = next((a for a in _actors() if a.get_actor_label()
                       == f"{actor.get_actor_label()}_{ROVER_SHADOW_CAPTURE}"), None)
        if helper is None:
            summary += "\n[-] Rover shadow: its shadow-map capture is missing; send again."
        install_watcher()
        summary += ("\nRover shadow: the editor watcher keeps its shadow map pointed at the "
                    "rover from the sun.")
    elif sun or tracks_on:
        install_watcher()
    elif not _in_pie():
        uninstall_watcher()
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
        uninstall_watcher()
    if not unreal.EditorAssetLibrary.does_asset_exist(BACKUP_MATERIAL):
        return ("Nothing to restore: no backup of the object-mask material."
                + (f" Removed {removed} rover-shadow helper capture(s)." if removed else ""))
    unreal.EditorAssetLibrary.delete_asset(MASK_MATERIAL)
    unreal.EditorAssetLibrary.rename_asset(BACKUP_MATERIAL, MASK_MATERIAL)
    return (f"Restored the original {MASK_MATERIAL}."
            + (f" Removed {removed} rover-shadow helper capture(s)." if removed else ""))
