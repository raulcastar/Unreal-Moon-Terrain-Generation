"""
Unreal Wheel Tracks
-------------------
Executes natively within the Unreal Engine Python environment. Leaves wheel
tracks on the Landscape behind a rover during Play In Editor.

A pool of track decals is placed in the level once, hidden. While the game
runs, the editor watcher (unreal_crater_mask) calls update_wheel_tracks every
frame: each wheel that touches the Landscape and has rolled on by
TRACK_SPACING_CM gets the next decal of the pool, pressed onto the ground there
along its direction of travel. The oldest decals are reused, so roughly
POOL_SIZE * TRACK_SPACING_CM / wheels of track stays behind each wheel. Tracks
start afresh each time Play starts.

The decal redraws the Landscape's own regolith texture at the same place, a
little darker and smoother, with a shallow rut and tread ribs, so tracks blend
into the ground; it also writes a small metallic value (TRACK_METALLIC) that regolith never
has (Landscape_Material leaves Metallic at 0). The object-mask material reads it
to tag track pixels with their own id.
"""

import math

import unreal

GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
TRACK_MATERIAL = f"{GROUND_TRUTH_FOLDER}/M_WheelTrackDecal"
POOL_TAG = "WheelTrack"          # Tag on every pool decal; a second tag names the rover
POOL_FOLDER = "WheelTracks"      # Outliner folder
POOL_SIZE = 2000
WHEEL_SUFFIX = "_Wheel"          # Rover components that leave tracks
TRACK_SPACING_CM = 12.0          # Distance between decals along a track (4 tread periods)
TRACK_LENGTH_CM = 18.0           # Each decal's length; overlaps the next for a continuous track
TRACK_DEPTH_CM = 8.0             # Projection depth, centred on the ground point (thin: ground only)
CLEARANCE_CM = 22.0              # A track piece is laid once every wheel is this far from it,
                                 # so no decal lands on a wheel (the rover is never modified)
MAX_PENDING = 256                # Track pieces waiting for the wheels to roll off
TREAD_PERIOD_CM = 3.0            # Tread rib spacing
TRACK_METALLIC = 0.05            # Mask marker; regolith is 0, visually negligible
CONTACT_TOLERANCE_CM = 3.0       # A wheel leaves a track within this of the ground
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
// Tread lug imprint: square lugs with sloped flanks, one per tread period.
#define LUG(f) (smoothstep(0.12, 0.28, f) - smoothstep(0.62, 0.78, f))
float lugF = frac(UV.x * Ribs);
float lug = LUG(lugF);
float slope = (LUG(frac(UV.x * Ribs + 0.02)) - lug) / 0.02;   // d(height)/d(period)
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
// Tread lug imprint: square lugs with sloped flanks, one per tread period.
#define LUG(f) (smoothstep(0.12, 0.28, f) - smoothstep(0.62, 0.78, f))
float lugF = frac(UV.x * Ribs);
float lug = LUG(lugF);
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


def _track_material():
    """
    The track decal material (created or rebuilt). It draws the Landscape's own
    regolith texture at the same place, a little darker, with the rut and tread,
    so a track blends into the ground around it.
    """
    lib = unreal.MaterialEditingLibrary
    material = unreal.load_asset(TRACK_MATERIAL)
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            TRACK_MATERIAL.rsplit("/", 1)[-1], GROUND_TRUTH_FOLDER, unreal.Material,
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
                     "FadeSharpness", "Shade", "UV", "Ribs", "LugShade"), -400, -400)
    ribs = scalar("Ribs", TRACK_LENGTH_CM / TREAD_PERIOD_CM, -700, 300)
    lib.connect_material_expressions(uv, "", colour, "UV")
    lib.connect_material_expressions(ribs, "", colour, "Ribs")
    lib.connect_material_expressions(scalar("LugShade", 0.08, -700, 40), "", colour, "LugShade")
    lib.connect_material_expressions(near, "RGB", colour, "Near")
    lib.connect_material_expressions(far, "RGB", colour, "Far")
    lib.connect_material_expressions(depth, "", colour, "Depth")
    for name, value, y in (("Desaturation", _landscape_scalar("Desaturation", 0.5), -260),
                           ("Whiteness", _landscape_scalar("Whiteness_Multiplier", 1.0), -200),
                           ("DistanceOffset", _landscape_scalar("DistanceOffset", 1000.0), -140),
                           ("FadeSharpness", _landscape_scalar("FadeSharpness", 5000.0), -80),
                           ("Shade", 0.7, -20)):
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
                     "DistanceOffset", "FadeSharpness", "Up", "Along", "Across"), -400, 300)
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
    lib.connect_material_expressions(scalar("LugSlope", 0.09, -700, 440), "", normal, "LugSlope")
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


def _wheels(actor):
    return [c for c in actor.get_components_by_class(unreal.StaticMeshComponent)
            if c.get_name().endswith(WHEEL_SUFFIX)]


def _wheel_size(wheel):
    """(radius, width) in cm: the mesh's two equal extents are the tyre, the third its width."""
    extent = wheel.get_editor_property("static_mesh").get_bounds().box_extent
    scale = wheel.get_world_scale()
    sizes = sorted((abs(extent.x * scale.x), abs(extent.y * scale.y), abs(extent.z * scale.z)))
    return sizes[2], 2.0 * sizes[0]


def _pool(world, owner_label=None):
    """Pool decals in `world` (optionally of one rover), in a stable order."""
    decals = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.DecalActor)
              if POOL_TAG in [str(t) for t in a.tags]
              and (owner_label is None or f"owner={owner_label}" in [str(t) for t in a.tags])]
    return sorted(decals, key=lambda a: a.get_actor_label())


def remove_wheel_tracks(world=None):
    """Deletes the track decals from the open level. Returns how many."""
    world = world or unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    decals = _pool(world)
    for decal in decals:
        decal.destroy_actor()
    return len(decals)


def setup_wheel_tracks(actor, pool_size=POOL_SIZE):
    """
    Places (or refreshes) the hidden track decals for `actor`'s wheels in the open
    level. Editor only. Returns (ready, summary line).
    """
    wheels = _wheels(actor)
    if not wheels:
        return False, f"Wheel tracks: off, '{actor.get_actor_label()}' has no '*{WHEEL_SUFFIX}' meshes."
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    material = _track_material()
    radius, width = _wheel_size(wheels[0])
    size = unreal.Vector(TRACK_DEPTH_CM, width, TRACK_LENGTH_CM)
    label = actor.get_actor_label()
    pool = _pool(world, label)
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
                  f"({width:.1f} cm wide, about {per_wheel:.0f} m kept per wheel); they appear "
                  "during Play.")


def _game_state(world, state):
    """
    Wheels, rover and pool of `world` (a running game), cached in `state`. Every
    Play reuses the same world name, so the cache is checked object by object.
    """
    valid = unreal.SystemLibrary.is_valid
    cached = ([state.get("track_rover")] + [w for w, _ in state.get("track_wheels", [])]
              + state.get("track_pool", [])[:1])
    if (state.get("track_world") == world.get_path_name() and cached[0] is not None
            and all(valid(o) for o in cached)):
        return state
    state.update(track_world=world.get_path_name(), track_wheels=[], track_next=0, track_last={},
                 track_pending=[])
    pool = _pool(world)
    if not pool:
        state["track_pool"] = []
        return state
    owner_label = next(str(t)[6:] for t in pool[0].tags if str(t).startswith("owner="))
    rover = next((a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
                  if a.get_actor_label() == owner_label), None)
    state["track_pool"] = pool
    state["track_rover"] = rover
    if rover is not None:
        # Read-only: never change the rover's components in a running game.
        # (Changing a simulating component's properties rebuilds its physics body
        # and breaks the wheel constraints.)
        state["track_wheels"] = [(w, _wheel_size(w)[0]) for w in _wheels(rover)]
    return state


def update_wheel_tracks(world, state):
    """Called every frame by the editor watcher while a game runs."""
    if world is None or not unreal.get_editor_subsystem(
            unreal.LevelEditorSubsystem).is_in_play_in_editor():
        return                                          # Tracks only in a running game
    state = _game_state(world, state)
    pool = state.get("track_pool")
    if not pool or not state["track_wheels"]:
        return
    ignore = [state["track_rover"]]
    centres = [wheel.get_world_location() for wheel, _ in state["track_wheels"]]
    pending = state["track_pending"]
    for (wheel, radius), centre in zip(state["track_wheels"], centres):
        key = wheel.get_name()
        last = state["track_last"].get(key)
        if last is None:
            state["track_last"][key] = centre
            continue
        travel = unreal.Vector(centre.x - last.x, centre.y - last.y, 0.0)
        distance = math.hypot(travel.x, travel.y)
        if distance < TRACK_SPACING_CM:
            continue
        direction = unreal.Vector(travel.x / distance, travel.y / distance, 0.0)
        steps = min(int(distance // TRACK_SPACING_CM), 8)     # Catch up after a fast frame
        for step in range(1, steps + 1):
            point = unreal.Vector(last.x + direction.x * TRACK_SPACING_CM * step,
                                  last.y + direction.y * TRACK_SPACING_CM * step, centre.z)
            hit = unreal.SystemLibrary.line_trace_single(
                world, point, point - unreal.Vector(0, 0, radius + 50.0),
                unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, ignore,
                unreal.DrawDebugTrace.NONE, True)
            if hit is None:
                continue
            parts = hit.to_tuple()
            blocking, gap, ground, normal, hit_actor = parts[0], parts[3], parts[5], parts[7], parts[9]
            if (not blocking or gap > radius + CONTACT_TOLERANCE_CM
                    or not isinstance(hit_actor, unreal.LandscapeProxy)):
                continue                                # In the air, or on a rock
            pending.append((ground, unreal.MathLibrary.make_rot_from_xz(
                unreal.Vector(-normal.x, -normal.y, -normal.z), direction)))
        state["track_last"][key] = unreal.Vector(last.x + direction.x * TRACK_SPACING_CM * steps,
                                                 last.y + direction.y * TRACK_SPACING_CM * steps,
                                                 centre.z) if steps < 8 else centre

    # Lay the pieces every wheel has rolled off (a back wheel may still be coming).
    del pending[:-MAX_PENDING]
    waiting = []
    for ground, rotation in pending:
        if any(math.hypot(c.x - ground.x, c.y - ground.y) < CLEARANCE_CM for c in centres):
            waiting.append((ground, rotation))
            continue
        decal = pool[state["track_next"] % len(pool)]
        state["track_next"] += 1
        decal.set_actor_location_and_rotation(ground, rotation, False, False)
        decal.set_actor_hidden_in_game(False)
    pending[:] = waiting
