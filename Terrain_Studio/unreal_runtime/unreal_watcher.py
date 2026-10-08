"""
Unreal Editor Watcher (Terrain Studio runtime)
----------------------------------------------
Executes natively within the Unreal Engine Python environment, inside the Unreal
project (<Project>/Content/Python/TerrainStudio; init_unreal.py starts it when
the editor opens). It needs only this folder: unreal_wheel_tracks.

A function registered as an editor tick that, on every editor frame:
  - points each rover's shadow-map capture (<actor>_RoverShadowCapture) at the
    rover from the sun, in the level and in a running game, and writes its frame
    to MPC_Sun (the rover's shadow in the shadow mask);
  - keeps MPC_Sun pointing at the level's sun (the sun's id in the object mask);
  - during Play, lays the wheel tracks.

It only reads the rover and moves its own objects. The studio's setup code
(unreal_commands/: unreal_ground_truth, unreal_track_setup)
creates those objects and uses the helpers here.
"""

import builtins

import unreal

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
SUN_MPC = f"{GROUND_TRUTH_FOLDER}/Sun/MPC_Sun"
SUN_PARAMETER = "SunDirection"   # xyz: unit vector towards the sun; w: cos(disc radius)
ROVER_SHADOW_CAPTURE = "RoverShadowCapture"   # Helper actor label: <actor>_RoverShadowCapture
# The rover's shadow map in MPC_Sun: the capture's position (w: half its width,
# cm) and its right and up axes. The capture looks from the sun at the mask
# camera (on the rover) from this far away.
SHADOW_MAP_PARAMETERS = ("RoverShadowOrigin", "RoverShadowRight", "RoverShadowUp")
SHADOW_MAP_DISTANCE_CM = 2000.0
WATCH_INTERVAL = 0.5       # Seconds between the watcher's helper checks
WATCH_RESCAN = 10.0        # Seconds between level scans while no helper is found


def alive(obj):
    """True if `obj` still exists. (is_valid raises, not returns False, for an object
    of an earlier Play session.)"""
    try:
        return obj is not None and unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


def sun_light(world):
    """The level's sun: the Directional Light linked to the sky atmosphere, else the first."""
    lights = list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.DirectionalLight))
    linked = [l for l in lights if l.light_component.get_editor_property("atmosphere_sun_light")]
    return (linked or lights or [None])[0]


def sun_vector(light):
    """LinearColor(towards-sun x, y, z, cos(disc radius)) for `light`."""
    import math
    forward = light.get_actor_forward_vector()
    radius = math.radians(light.light_component.get_editor_property("light_source_angle") / 2.0)
    return unreal.LinearColor(-forward.x, -forward.y, -forward.z, math.cos(max(radius, 1e-4)))


def update_sun_mask(world, state=None):
    """Points MPC_Sun in `world` at its sun; skips the write when nothing changed."""
    state = state if state is not None else {}
    collection = state.get("sun_collection") or unreal.load_asset(SUN_MPC)
    if collection is None:
        return
    state["sun_collection"] = collection
    light = state.get("sun_light")
    if not alive(light) or state.get("sun_world") != world:
        light = state["sun_light"] = sun_light(world)
        state["sun_world"], state["sun_value"] = world, None
    if light is None:
        return
    value = sun_vector(light)
    key = tuple(round(x, 6) for x in (value.r, value.g, value.b, value.a))
    if key != state.get("sun_value"):
        unreal.MaterialLibrary.set_vector_parameter_value(world, collection, SUN_PARAMETER, value)
        state["sun_value"] = key


def find_streamer(actor):
    for component in actor.get_components_by_class(unreal.ActorComponent):
        if component.get_class().get_name() == "StereoCamStreamer":
            return component
    return None


def place_shadow_map(world, helper, capture, state=None):
    """
    Points the rover's shadow-map capture `helper` at the rover from the sun:
    looking along the sunlight at the mask camera `capture` (it rides the rover's
    body, also when that moves away from the actor's root in play), from
    SHADOW_MAP_DISTANCE_CM towards the sun, and writes that frame to MPC_Sun for
    the mask material. Moves only the helper; the rover is only read.
    """
    state = state if state is not None else {}
    update_sun_mask(world, state)
    light, collection = state.get("sun_light"), state.get("sun_collection")
    if light is None or collection is None:
        return
    if helper.get_editor_property("root_component").get_attach_parent() is not None:
        rule = unreal.DetachmentRule.KEEP_WORLD
        helper.detach_from_actor(rule, rule, rule)     # An earlier version attached it
    forward = light.get_actor_forward_vector()          # Along the sunlight
    centre = capture.get_world_location()
    position = unreal.Vector(centre.x - forward.x * SHADOW_MAP_DISTANCE_CM,
                             centre.y - forward.y * SHADOW_MAP_DISTANCE_CM,
                             centre.z - forward.z * SHADOW_MAP_DISTANCE_CM)
    # Only when it moved (more than a millimetre or a hundredth of a degree): in
    # the editor, moving an actor marks the level as changed.
    key = tuple(round(x, 1) for x in (position.x, position.y, position.z)) + tuple(
        round(x, 4) for x in (forward.x, forward.y, forward.z))
    placed = state.setdefault("map_placed", {})
    if placed.get(helper.get_path_name()) == key:
        return
    placed[helper.get_path_name()] = key
    helper.set_actor_location_and_rotation(
        position, unreal.MathLibrary.make_rot_from_x(forward), False, False)
    component = helper.capture_component2d
    half = 0.5 * component.get_editor_property("ortho_width")
    right, up = component.get_right_vector(), component.get_up_vector()
    for name, vector, w in zip(SHADOW_MAP_PARAMETERS, (position, right, up), (half, 0.0, 0.0)):
        unreal.MaterialLibrary.set_vector_parameter_value(
            world, collection, name, unreal.LinearColor(vector.x, vector.y, vector.z, w))


def _helper_pairs(world):
    """
    (helper, mask capture) for every rover shadow-map capture in `world`. Looks
    only at SceneCapture2D actors (a few) for the helpers; their rover is named in
    their label.
    """
    pairs, everything = [], None
    for helper in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.SceneCapture2D):
        label = helper.get_actor_label()
        if not label.endswith("_" + ROVER_SHADOW_CAPTURE):
            continue
        owner_label = label[:-len(ROVER_SHADOW_CAPTURE) - 1]
        # The rover is a Pawn: a few actors, rather than the whole level (which can
        # hold thousands of rocks); everything only if it is not one.
        owner = next((a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Pawn)
                      if a.get_actor_label() == owner_label), None)
        if owner is None:
            if everything is None:
                everything = {a.get_actor_label(): a for a in
                              unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)}
            owner = everything.get(owner_label)
        streamer = find_streamer(owner) if owner else None
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
    Editor tick: every frame, points each rover's shadow-map capture at it from
    the sun and keeps MPC_Sun pointing at the sun (in the level and in a running
    game), and, in a running game, lays wheel tracks.
    """
    state = getattr(builtins, "_terrain_studio_watcher", None)
    if state is None:
        return
    try:
        import unreal_wheel_tracks
        game = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_game_world()
        if game is not None:
            unreal_wheel_tracks.update_wheel_tracks(game, state)
        state["track_error"] = None
    except Exception as error:          # Report each new error once; rebuild the cache
        if state.get("track_error") != str(error):
            unreal.log_warning(f"Wheel tracks: {error}")
        state["track_error"], state["track_world"] = str(error), None
    try:                                # Every frame, as the rover moves
        editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = editor.get_game_world() or editor.get_editor_world()
        if world is not None and state["world"] == world.get_path_name():
            for helper, capture in state["pairs"]:
                if alive(helper) and alive(capture):
                    place_shadow_map(world, helper, capture, state)
        state["map_error"] = None
    except Exception as error:          # Report each new error once
        if state.get("map_error") != str(error):
            unreal.log_warning(f"Rover shadow map: {error}")
        state["map_error"] = str(error)
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
        valid = alive
        stale = (state["world"] != world.get_path_name()
                 or not all(valid(h) and valid(c) for h, c in state["pairs"])
                 or (not state["pairs"] and state["since_scan"] >= WATCH_RESCAN))
        if stale:
            state["pairs"] = _helper_pairs(world)
            state["world"] = world.get_path_name()
            state["since_scan"] = 0.0
        update_sun_mask(world, state)
        state["failed"] = False
    except Exception as error:      # Never let the tick raise; report once
        if not state.get("failed"):
            unreal.log_warning(f"Rover shadow watcher: {error}")
        state["failed"] = True


def install_watcher():
    """
    Registers the attachment watcher for this editor session (replacing an
    earlier one). It lasts until the editor closes; sending masks or Find in the
    studio installs it again.
    """
    uninstall_watcher()
    state = {"elapsed": 0.0, "since_scan": 0.0, "world": None, "pairs": [], "failed": False}
    state["handle"] = unreal.register_slate_post_tick_callback(_watch_tick)
    builtins._terrain_studio_watcher = state


def uninstall_watcher():
    """Unregisters the watcher (and one registered under its earlier name)."""
    for name in ("_terrain_studio_watcher", "_rover_shadow_watch"):
        state = getattr(builtins, name, None)
        if state is not None:
            unreal.unregister_slate_post_tick_callback(state["handle"])
            setattr(builtins, name, None)
