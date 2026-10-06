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

The decal shades a shallow rut with tread ribs, darkens and smooths the
regolith, and writes a small metallic value (TRACK_METALLIC) that regolith never
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
return (1.0 - smoothstep(0.75, 1.0, across)) * (1.0 - smoothstep(0.55, 1.0, along));"""

NORMAL_CODE = r"""// A shallow rut across the track and tread ribs along it (tangent space).
float across = (UV.y - 0.5) * 2.0;
float phase = UV.x * Ribs * 6.2831853;
return normalize(float3(-cos(phase) * RibDepth, -across * Rut, 1.0));"""


def _track_material():
    """The track decal material (created or rebuilt)."""
    lib = unreal.MaterialEditingLibrary
    material = unreal.load_asset(TRACK_MATERIAL)
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            TRACK_MATERIAL.rsplit("/", 1)[-1], GROUND_TRUTH_FOLDER, unreal.Material,
            unreal.MaterialFactoryNew())
    material.set_editor_property("material_domain", unreal.MaterialDomain.MD_DEFERRED_DECAL)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)   # Decal: colour, normal, roughness, metallic
    lib.delete_all_material_expressions(material)

    uv = lib.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -900, 0)

    def custom(code, output, inputs, y):
        node = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -500, y)
        node.set_editor_property("code", code)
        node.set_editor_property("output_type", output)
        entries = []
        for name in inputs:
            entry = unreal.CustomInput()
            entry.set_editor_property("input_name", name)
            entries.append(entry)
        node.set_editor_property("inputs", entries)
        lib.connect_material_expressions(uv, "", node, "UV")
        return node

    def scalar(name, value, y):
        node = lib.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -800, y)
        node.set_editor_property("parameter_name", name)
        node.set_editor_property("default_value", value)
        return node

    opacity = custom(OPACITY_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT1, ("UV",), 0)
    normal = custom(NORMAL_CODE, unreal.CustomMaterialOutputType.CMOT_FLOAT3,
                    ("UV", "Ribs", "Rut", "RibDepth"), 200)
    lib.connect_material_expressions(
        scalar("Ribs", TRACK_LENGTH_CM / TREAD_PERIOD_CM, 260), "", normal, "Ribs")
    lib.connect_material_expressions(scalar("Rut", 0.2, 330), "", normal, "Rut")
    lib.connect_material_expressions(scalar("RibDepth", 0.2, 400), "", normal, "RibDepth")

    colour = lib.create_material_expression(material, unreal.MaterialExpressionVectorParameter, -500, -200)
    colour.set_editor_property("parameter_name", "TrackColour")
    colour.set_editor_property("default_value", unreal.LinearColor(0.11, 0.108, 0.104, 1.0))
    strength = scalar("TrackOpacity", 0.85, 80)
    multiply = lib.create_material_expression(material, unreal.MaterialExpressionMultiply, -250, 0)
    lib.connect_material_expressions(opacity, "", multiply, "A")
    lib.connect_material_expressions(strength, "", multiply, "B")

    lib.connect_material_property(colour, "", unreal.MaterialProperty.MP_BASE_COLOR)
    lib.connect_material_property(normal, "", unreal.MaterialProperty.MP_NORMAL)
    lib.connect_material_property(scalar("Roughness", 0.8, 480), "", unreal.MaterialProperty.MP_ROUGHNESS)
    lib.connect_material_property(scalar("MaskMarker", TRACK_METALLIC, 550), "",
                                  unreal.MaterialProperty.MP_METALLIC)
    lib.connect_material_property(multiply, "", unreal.MaterialProperty.MP_OPACITY)
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
