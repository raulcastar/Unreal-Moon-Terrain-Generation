"""
Unreal Wheel Tracks: setup (Terrain Studio)
-------------------------------------------
Executes natively within the Unreal Engine Python environment, called by the
studio when it sends masks. Places the hidden pool of track decals for a rover
in the level and builds the track decal material with the chosen tread look; the
editor watcher's unreal_wheel_tracks lays them during Play.

The decal redraws the Landscape's own regolith texture at the same place, a
little darker and smoother, with a shallow rut and the tread, so tracks blend
into the ground; it also writes a small metallic value (TRACK_METALLIC) that
regolith never has (Landscape_Material leaves Metallic at 0). The object-mask
material reads it to tag track pixels with their own id.
"""

import unreal

from unreal_wheel_tracks import POOL_TAG, TRACK_SPACING_CM, WHEEL_SUFFIX, rover_wheels, track_pool, wheel_size

GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
TRACK_MATERIAL = f"{GROUND_TRUTH_FOLDER}/WheelTracks/M_WheelTrackDecal"
POOL_FOLDER = "WheelTracks"      # Outliner folder
POOL_SIZE = 2000
TRACK_LENGTH_CM = 18.0           # Each decal's length; overlaps the next for a continuous track
TRACK_DEPTH_CM = 8.0             # Projection depth, centred on the ground point (thin: ground only)
TREAD_PERIOD_CM = 3.0            # Default tread lug spacing
# Tread look, chosen in Terrain Studio. Lug spacings divide TRACK_SPACING_CM, so the
# tread stays continuous from one decal to the next.
TRACK_PATTERNS = {"straight": 0, "chevron": 1, "blocks": 2, "smooth": 3}
LUG_SPACINGS_CM = (2.0, 3.0, 4.0, 6.0)
DEFAULT_STYLE = {"pattern": "straight", "period_cm": TREAD_PERIOD_CM, "relief": 1.0, "shade": 0.7}
TRACK_METALLIC = 0.05            # Mask marker; regolith is 0, visually negligible
HIDDEN_Z_CM = -1.0e6             # Where unused decals wait
# Decal UVs: x along the track (0..1 over its length), y across it (0..1 over its width).
OPACITY_CODE = r"""// Full across the wheel's width with soft edges, faded at the ends so
// overlapping decals blend into one continuous track.
float across = abs(UV.y - 0.5) * 2.0;
float along = abs(UV.x - 0.5) * 2.0;
return (1.0 - smoothstep(0.7, 1.0, across)) * (1.0 - smoothstep(0.55, 1.0, along));"""
NORMAL_CODE = r"""// World-space normal. The regolith's relief as Landscape_Material shades it (its
// normal map, near and far tilings blended by distance, in the Landscape's
// tangent frame: +X, +Y, up) on the slope under the decal, pressed flatter by
// the wheel, plus a shallow rut across the track and the tread's lug imprints.
float t = saturate((Depth - DistanceOffset) / FadeSharpness);
float3 g = lerp(GroundNear, GroundFar, t);
g = normalize(float3(g.xy * Grain, g.z));
float3 N = normalize(Up);
float3 T = normalize(float3(1.0, 0.0, 0.0) - N * N.x);
float3 B = cross(N, T);
float3 n = g.x * T + g.y * B + g.z * N;
float across = (UV.y - 0.5) * 2.0;
// Tread lug imprint, one per tread period, square with sloped flanks. Pattern:
// 0 straight lugs, 1 chevron, 2 staggered blocks, 3 smooth (no tread).
#define LUG(f) (Pattern > 2.5 ? 0.0 : smoothstep(0.12, 0.28, f) - smoothstep(0.62, 0.78, f))
float lugAcross = (UV.y - 0.5) * 2.0;
float lugShift = Pattern < 0.5 ? 0.0 : (Pattern < 1.5 ? abs(lugAcross) * 0.5
                                                       : (lugAcross > 0.0 ? 0.5 : 0.0));
float lugGap = (Pattern > 1.5 && Pattern < 2.5) ? smoothstep(0.06, 0.16, abs(lugAcross)) : 1.0;
float lug = LUG(frac(UV.x * Ribs + lugShift)) * lugGap;
float slope = (LUG(frac(UV.x * Ribs + lugShift + 0.02)) * lugGap - lug) / 0.02;   // d(height)/d(period)
float edge = 1.0 - smoothstep(0.8, 0.95, abs(across));           // lugs stop short of the edge
n += normalize(Along) * (-slope * LugSlope * edge) + normalize(Across) * (-across * Rut);
return normalize(n);"""
# Landscape_Material's roughness (near and far tilings blended by distance),
# a little smoother where the wheel packed the regolith.
ROUGHNESS_CODE = r"""float t = saturate((Depth - DistanceOffset) / FadeSharpness);
return lerp(Near, Far, t) * Compaction;"""
# The Landscape's texture coordinates at this pixel, as LandscapeLayerCoords
# gives them (quads from the Landscape's origin), divided by a tiling.
COORDS_CODE = r"""return (Pos.xy - float2(OriginX, OriginY)) / (float2(ScaleX, ScaleY) * Tiling);"""
# Landscape_Material's colour: desaturated regolith texture, near and far tilings
# blended by distance, then the track's darkening.
COLOUR_CODE = r"""float3 lum = float3(0.3, 0.59, 0.11);
float3 n = lerp(Near, dot(Near, lum).xxx, Desaturation);
float3 f = lerp(Far, dot(Far, lum).xxx, Desaturation);
float t = saturate((Depth - DistanceOffset) / FadeSharpness);
// Tread lug imprint, one per tread period, square with sloped flanks. Pattern:
// 0 straight lugs, 1 chevron, 2 staggered blocks, 3 smooth (no tread).
#define LUG(f) (Pattern > 2.5 ? 0.0 : smoothstep(0.12, 0.28, f) - smoothstep(0.62, 0.78, f))
float lugAcross = (UV.y - 0.5) * 2.0;
float lugShift = Pattern < 0.5 ? 0.0 : (Pattern < 1.5 ? abs(lugAcross) * 0.5
                                                       : (lugAcross > 0.0 ? 0.5 : 0.0));
float lugGap = (Pattern > 1.5 && Pattern < 2.5) ? smoothstep(0.06, 0.16, abs(lugAcross)) : 1.0;
float lug = LUG(frac(UV.x * Ribs + lugShift)) * lugGap;
float edge = 1.0 - smoothstep(0.8, 0.95, abs((UV.y - 0.5) * 2.0));
return lerp(n, f, t) * Whiteness * Shade * (1.0 - LugShade * lug * edge);"""
LANDSCAPE_MATERIAL = "/Game/Landscape/Landscape_Material"
REGOLITH_TEXTURES = "/Game/Landscape/Assets/moon_01_4k/Textures"
NEAR_TILING, FAR_TILING = 2.5, 100.0      # Landscape_Material's LandscapeLayerCoords scales


def _landscape_frame():
    """(origin x, origin y, scale x, scale y) in cm of the level's Landscape, or a default."""
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = editor.get_game_world() or editor.get_editor_world()
    landscapes = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.LandscapeProxy)
    if not landscapes:
        return 0.0, 0.0, 100.0, 100.0
    location, scale = landscapes[0].get_actor_location(), landscapes[0].get_actor_scale3d()
    return location.x, location.y, scale.x, scale.y


def _landscape_scalar(name, default):
    material = unreal.load_asset(LANDSCAPE_MATERIAL)
    if material is None:
        return default
    try:
        return unreal.MaterialEditingLibrary.get_material_default_scalar_parameter_value(material, name)
    except Exception:
        return default


def _landscape_specular():
    """Landscape_Material's constant Specular (regolith: 0), so tracks do not shine."""
    material = unreal.load_asset(LANDSCAPE_MATERIAL)
    node = material and unreal.MaterialEditingLibrary.get_material_property_input_node(
        material, unreal.MaterialProperty.MP_SPECULAR)
    try:
        return float(node.get_editor_property("r"))
    except Exception:
        return 0.5 if node is None and material is None else 0.0


def _style(style):
    """DEFAULT_STYLE updated with `style`, checked: known pattern, a lug spacing that divides the decal spacing."""
    merged = dict(DEFAULT_STYLE, **(style or {}))
    if merged["pattern"] not in TRACK_PATTERNS:
        merged["pattern"] = DEFAULT_STYLE["pattern"]
    merged["period_cm"] = min(LUG_SPACINGS_CM, key=lambda p: abs(p - float(merged["period_cm"])))
    merged["relief"] = max(0.0, min(3.0, float(merged["relief"])))
    merged["shade"] = max(0.3, min(1.0, float(merged["shade"])))
    return merged


def _track_material(style=None):
    """
    The track decal material (created or rebuilt) with the tread look `style`
    (see DEFAULT_STYLE). It draws the Landscape's own regolith texture at the
    same place, a little darker, with the rut and tread, so a track blends into
    the ground around it.
    """
    style = _style(style)
    lib = unreal.MaterialEditingLibrary
    material = unreal.load_asset(TRACK_MATERIAL)
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            TRACK_MATERIAL.rsplit("/", 1)[-1], TRACK_MATERIAL.rsplit("/", 1)[0], unreal.Material,
            unreal.MaterialFactoryNew())
    material.set_editor_property("material_domain", unreal.MaterialDomain.MD_DEFERRED_DECAL)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)   # Decal: colour, normal, roughness, metallic
    material.set_editor_property("tangent_space_normal", False)    # The normal is built in world space
    lib.delete_all_material_expressions(material)

    uv = lib.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -1400, 0)
    position = lib.create_material_expression(material, unreal.MaterialExpressionWorldPosition, -1400, -400)
    depth = lib.create_material_expression(material, unreal.MaterialExpressionPixelDepth, -1400, -600)

    def custom(code, output, inputs, x, y):
        node = lib.create_material_expression(material, unreal.MaterialExpressionCustom, x, y)
        node.set_editor_property("code", code)
        node.set_editor_property("output_type", output)
        entries = []
        for name in inputs:
            entry = unreal.CustomInput()
            entry.set_editor_property("input_name", name)
            entries.append(entry)
        node.set_editor_property("inputs", entries)
        return node

    def scalar(name, value, x, y):
        node = lib.create_material_expression(material, unreal.MaterialExpressionScalarParameter, x, y)
        node.set_editor_property("parameter_name", name)
        node.set_editor_property("default_value", float(value))
        return node

    def texture(name, sampler, coords, y):
        node = lib.create_material_expression(material, unreal.MaterialExpressionTextureSample, -700, y)
        node.set_editor_property("texture", unreal.load_asset(f"{REGOLITH_TEXTURES}/{name}"))
        node.set_editor_property("sampler_type", sampler)
        lib.connect_material_expressions(coords, "", node, "UVs")
        return node

    # Landscape coordinates (near and far tilings)
    origin_x, origin_y, scale_x, scale_y = _landscape_frame()
    frame = [scalar("LandscapeOriginX", origin_x, -1400, -900),
             scalar("LandscapeOriginY", origin_y, -1400, -830),
             scalar("LandscapeScaleX", scale_x, -1400, -760),
             scalar("LandscapeScaleY", scale_y, -1400, -690)]
    coords = []
    for tiling, y in ((NEAR_TILING, -500), (FAR_TILING, -300)):
        node = custom(COORDS_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT2,
                      ("Pos", "OriginX", "OriginY", "ScaleX", "ScaleY", "Tiling"), -1000, y)
        lib.connect_material_expressions(position, "", node, "Pos")
        for source, name in zip(frame, ("OriginX", "OriginY", "ScaleX", "ScaleY")):
            lib.connect_material_expressions(source, "", node, name)
        lib.connect_material_expressions(scalar(f"Tiling{int(tiling * 10)}", tiling, -1200, y),
                                         "", node, "Tiling")
        coords.append(node)

    colour_sampler = unreal.MaterialSamplerType.SAMPLERTYPE_COLOR
    near = texture("moon_01_diff", colour_sampler, coords[0], -500)
    far = texture("moon_01_diff", colour_sampler, coords[1], -300)
    linear, normal_map = (unreal.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR,
                          unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    rough_near = texture("moon_01_rough", linear, coords[0], -100)
    rough_far = texture("moon_01_rough", linear, coords[1], 0)
    relief_near = texture("moon_01_nor_gl", normal_map, coords[0], 600)
    relief_far = texture("moon_01_nor_gl", normal_map, coords[1], 700)

    def decal_axis(x, y, z, top):
        """A decal-space axis in world space (the decal's own transform)."""
        constant = lib.create_material_expression(material, unreal.MaterialExpressionConstant3Vector, -1000, top)
        constant.set_editor_property("constant", unreal.LinearColor(x, y, z, 0.0))
        transform = lib.create_material_expression(material, unreal.MaterialExpressionTransform, -800, top)
        transform.set_editor_property("transform_source_type",
                                      unreal.MaterialVectorCoordTransformSource.TRANSFORMSOURCE_LOCAL)
        transform.set_editor_property("transform_type", unreal.MaterialVectorCoordTransform.TRANSFORM_WORLD)
        lib.connect_material_expressions(constant, "", transform, "")
        return transform

    # Decals are placed with local X into the ground, Z along the track, Y across it.
    up, along, across = decal_axis(-1, 0, 0, 800), decal_axis(0, 0, 1, 880), decal_axis(0, 1, 0, 960)

    colour = custom(COLOUR_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT3,
                    ("Near", "Far", "Depth", "Desaturation", "Whiteness", "DistanceOffset",
                     "FadeSharpness", "Shade", "UV", "Ribs", "LugShade", "Pattern"), -400, -400)
    ribs = scalar("Ribs", TRACK_LENGTH_CM / style["period_cm"], -700, 300)
    pattern = scalar("Pattern", TRACK_PATTERNS[style["pattern"]], -700, 160)
    lib.connect_material_expressions(uv, "", colour, "UV")
    lib.connect_material_expressions(ribs, "", colour, "Ribs")
    lib.connect_material_expressions(pattern, "", colour, "Pattern")
    lib.connect_material_expressions(scalar("LugShade", 0.08 * style["relief"], -700, 40), "",
                                     colour, "LugShade")
    lib.connect_material_expressions(near, "RGB", colour, "Near")
    lib.connect_material_expressions(far, "RGB", colour, "Far")
    lib.connect_material_expressions(depth, "", colour, "Depth")
    for name, value, y in (("Desaturation", _landscape_scalar("Desaturation", 0.5), -260),
                           ("Whiteness", _landscape_scalar("Whiteness_Multiplier", 1.0), -200),
                           ("DistanceOffset", _landscape_scalar("DistanceOffset", 1000.0), -140),
                           ("FadeSharpness", _landscape_scalar("FadeSharpness", 5000.0), -80),
                           ("Shade", style["shade"], -20)):
        lib.connect_material_expressions(scalar(name, value, -700, y), "", colour, name)

    distance = {name: scalar(name, value, -1200, y) for name, value, y in (
        ("NormalDistanceOffset", _landscape_scalar("DistanceOffset", 1000.0), 1040),
        ("NormalFadeSharpness", _landscape_scalar("FadeSharpness", 5000.0), 1110))}
    roughness = custom(ROUGHNESS_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT1,
                       ("Near", "Far", "Depth", "DistanceOffset", "FadeSharpness", "Compaction"),
                       -400, -100)
    lib.connect_material_expressions(rough_near, "R", roughness, "Near")
    lib.connect_material_expressions(rough_far, "R", roughness, "Far")
    lib.connect_material_expressions(depth, "", roughness, "Depth")
    lib.connect_material_expressions(distance["NormalDistanceOffset"], "", roughness, "DistanceOffset")
    lib.connect_material_expressions(distance["NormalFadeSharpness"], "", roughness, "FadeSharpness")
    lib.connect_material_expressions(scalar("Compaction", 0.9, -700, 60), "", roughness, "Compaction")

    opacity = custom(OPACITY_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT1, ("UV",), -700, 200)
    lib.connect_material_expressions(uv, "", opacity, "UV")
    normal = custom(NORMAL_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT3,
                    ("UV", "Ribs", "Rut", "LugSlope", "GroundNear", "GroundFar", "Grain", "Depth",
                     "DistanceOffset", "FadeSharpness", "Up", "Along", "Across", "Pattern"), -400, 300)
    lib.connect_material_expressions(pattern, "", normal, "Pattern")
    lib.connect_material_expressions(uv, "", normal, "UV")
    lib.connect_material_expressions(relief_near, "RGB", normal, "GroundNear")
    lib.connect_material_expressions(relief_far, "RGB", normal, "GroundFar")
    lib.connect_material_expressions(scalar("Grain", 0.6, -700, 510), "", normal, "Grain")
    lib.connect_material_expressions(depth, "", normal, "Depth")
    lib.connect_material_expressions(distance["NormalDistanceOffset"], "", normal, "DistanceOffset")
    lib.connect_material_expressions(distance["NormalFadeSharpness"], "", normal, "FadeSharpness")
    for node, name in ((up, "Up"), (along, "Along"), (across, "Across")):
        lib.connect_material_expressions(node, "", normal, name)
    lib.connect_material_expressions(ribs, "", normal, "Ribs")
    lib.connect_material_expressions(scalar("Rut", 0.2, -700, 370), "", normal, "Rut")
    lib.connect_material_expressions(scalar("LugSlope", 0.09 * style["relief"], -700, 440), "",
                                     normal, "LugSlope")
    strength = lib.create_material_expression(material, unreal.MaterialExpressionMultiply, -200, 200)
    lib.connect_material_expressions(opacity, "", strength, "A")
    lib.connect_material_expressions(scalar("TrackOpacity", 0.95, -400, 220), "", strength, "B")

    lib.connect_material_property(colour, "", unreal.MaterialProperty.MP_BASE_COLOR)
    lib.connect_material_property(normal, "", unreal.MaterialProperty.MP_NORMAL)
    lib.connect_material_property(roughness, "", unreal.MaterialProperty.MP_ROUGHNESS)
    lib.connect_material_property(scalar("Specular", _landscape_specular(), -400, 600), "",
                                  unreal.MaterialProperty.MP_SPECULAR)
    lib.connect_material_property(scalar("MaskMarker", TRACK_METALLIC, -400, 520), "",
                                  unreal.MaterialProperty.MP_METALLIC)
    lib.connect_material_property(strength, "", unreal.MaterialProperty.MP_OPACITY)
    lib.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def remove_wheel_tracks(world=None):
    """Deletes the track decals from the open level. Returns how many."""
    world = world or unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    decals = track_pool(world)
    for decal in decals:
        decal.destroy_actor()
    return len(decals)


def describe_style(style):
    style = _style(style)
    return (f"{style['pattern']} tread, lugs every {style['period_cm']:g} cm, relief "
            f"{style['relief']:g}, regolith x{style['shade']:g}")


def apply_track_style(style=None):
    """Rebuilds the track decal material with the tread look `style`. Returns a summary line."""
    _track_material(style)
    return f"Wheel tracks: {describe_style(style)} ({TRACK_MATERIAL})."


def setup_wheel_tracks(actor, pool_size=POOL_SIZE, style=None):
    """
    Places (or refreshes) the hidden track decals for `actor`'s wheels in the open
    level, with the tread look `style`. Editor only. Returns (ready, summary line).
    """
    wheels = rover_wheels(actor)
    if not wheels:
        return False, f"Wheel tracks: off, '{actor.get_actor_label()}' has no '*{WHEEL_SUFFIX}' meshes."
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    material = _track_material(style)
    radius, width = wheel_size(wheels[0])
    size = unreal.Vector(TRACK_DEPTH_CM, width, TRACK_LENGTH_CM)
    label = actor.get_actor_label()
    pool = track_pool(world, label)
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    with unreal.ScopedSlowTask(max(pool_size - len(pool), 1), "Placing wheel-track decals") as task:
        task.make_dialog(False)
        while len(pool) < pool_size:
            decal = actors.spawn_actor_from_class(unreal.DecalActor, unreal.Vector(0, 0, HIDDEN_Z_CM))
            decal.set_actor_label(f"{label}_WheelTrack_{len(pool):04d}")
            decal.tags = [POOL_TAG, f"owner={label}"]
            decal.set_folder_path(POOL_FOLDER)
            pool.append(decal)
            task.enter_progress_frame(1)
    for decal in pool[pool_size:]:
        decal.destroy_actor()
    for decal in pool[:pool_size]:
        component = decal.get_component_by_class(unreal.DecalComponent)
        component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
        component.set_editor_property("decal_material", material)
        component.set_editor_property("decal_size", size)
        component.set_editor_property("fade_screen_size", 0.0005)
        decal.set_actor_hidden_in_game(True)
        decal.set_actor_location(unreal.Vector(0, 0, HIDDEN_Z_CM), False, False)
    per_wheel = pool_size * TRACK_SPACING_CM / len(wheels) / 100.0
    return True, (f"Wheel tracks: {pool_size} decals for {len(wheels)} wheels of {label} "
                  f"({width:.1f} cm wide, about {per_wheel:.0f} m kept per wheel; "
                  f"{describe_style(style)}); they appear during Play.")
