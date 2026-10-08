"""
Unreal Editor Watcher (Terrain Studio runtime)
----------------------------------------------
Executes natively within the Unreal Engine Python environment, inside the Unreal
project (<Project>/Content/Python/TerrainStudio; init_unreal.py starts it when
the editor opens). It needs only this folder: unreal_wheel_tracks.

A function registered as an editor tick that, on every editor frame:
  - keeps each rover-shadow helper capture (<actor>_RoverShadowCapture)
    attached to its mask camera, in the level and in a running game (Blueprint
    recompiles, also the one that can run when Play starts, re-attach it to the
    actor's root), every WATCH_INTERVAL seconds;
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
#   RoverShadow/RT_SunOnly_RoverHidden  helper capture's sun-only render without the rover
#   Sun/MPC_Sun                      sun direction and disc size for the sun's id
#   WheelTracks/M_WheelTrackDecal    wheel-track decal material (unreal_track_setup)
GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
SUN_MPC = f"{GROUND_TRUTH_FOLDER}/Sun/MPC_Sun"
SUN_PARAMETER = "SunDirection"   # xyz: unit vector towards the sun; w: cos(disc radius)
ROVER_SHADOW_CAPTURE = "RoverShadowCapture"   # Helper actor label: <actor>_RoverShadowCapture
WATCH_INTERVAL = 0.5       # Seconds between the watcher's attachment checks
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


def attach_helper(helper, capture):
    """
    Snaps the helper onto the mask camera. The camera may ride a physics body
    that moves away from the actor's root in play, so the helper must follow the
    camera itself. Recompiling the actor's Blueprint can re-attach it to the
    root; sending again fixes that. Returns True when attached to `capture`.
    """
    rule = unreal.AttachmentRule.SNAP_TO_TARGET
    helper.attach_to_component(capture, "", rule, rule, rule, False)
    return helper.get_editor_property("root_component").get_attach_parent() == capture


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
    Editor tick: keeps each rover-shadow helper attached to its mask camera, in
    the level and in a running game (Blueprint recompiles, also the one that can
    run when Play starts, re-attach it to the actor's root), keeps MPC_Sun
    pointing at the sun, and, every frame of a running game, lays wheel tracks.
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
        for helper, capture in state["pairs"]:
            if helper.get_editor_property("root_component").get_attach_parent() != capture:
                attached = attach_helper(helper, capture)
                unreal.log(f"Rover shadow: re-attached {helper.get_actor_label()} to "
                           f"{capture.get_name()}" + ("" if attached else " (failed)"))
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
