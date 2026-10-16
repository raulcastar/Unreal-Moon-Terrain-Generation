"""
Unreal Wheel Tracks (Terrain Studio runtime)
--------------------------------------------
Runs inside the Unreal Editor. Lays wheel tracks
on the Landscape behind a rover during Play In Editor; called every frame by the
editor watcher (unreal_watcher).

The pool of track decals is placed in the level beforehand by the studio
(unreal_commands/unreal_track_setup.py), hidden. Each wheel that touches
the Landscape and has rolled on by TRACK_SPACING_CM gets the next decal of the
pool, pressed onto the ground along its direction of travel, once every wheel
has rolled off that spot (so no track lands on a wheel). The oldest decals are
reused; tracks start afresh each time Play starts. The rover is only read.
"""

import math

import unreal

POOL_TAG = "WheelTrack"          # Tag on every pool decal; a second tag names the rover
WHEEL_SUFFIX = "_Wheel"          # Rover components that leave tracks
TRACK_SPACING_CM = 12.0          # Distance between decals along a track (4 tread periods)
CLEARANCE_CM = 22.0              # A track piece is laid once every wheel is this far from it,
MAX_PENDING = 256                # Track pieces waiting for the wheels to roll off
CONTACT_TOLERANCE_CM = 3.0       # A wheel leaves a track within this of the ground
SETTLE_TIME_S = 3.0              # After Play starts the rover settles onto the ground; its
                                 # wheels leave no tracks until then


def rover_wheels(actor):
    return [c for c in actor.get_components_by_class(unreal.StaticMeshComponent)
            if c.get_name().endswith(WHEEL_SUFFIX)]


def wheel_size(wheel):
    """(radius, width) in cm: the mesh's two equal extents are the tyre, the third its width."""
    extent = wheel.get_editor_property("static_mesh").get_bounds().box_extent
    scale = wheel.get_world_scale()
    sizes = sorted((abs(extent.x * scale.x), abs(extent.y * scale.y), abs(extent.z * scale.z)))
    return sizes[2], 2.0 * sizes[0]


def track_pool(world, owner_label=None):
    """Pool decals in `world` (optionally of one rover), in a stable order."""
    decals = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.DecalActor)
              if POOL_TAG in [str(t) for t in a.tags]
              and (owner_label is None or f"owner={owner_label}" in [str(t) for t in a.tags])]
    return sorted(decals, key=lambda a: a.get_actor_label())


def _alive(obj):
    """True if `obj` still exists. (is_valid raises, not returns False, for an object
    of an earlier Play session.)"""
    try:
        return obj is not None and unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


def _game_state(world, state):
    """
    Wheels, rover and pool of `world` (a running game), cached in `state`. Every
    Play reuses the same world name, so the cache is checked object by object.
    """
    valid = _alive
    cached = ([state.get("track_rover")] + [w for w, _ in state.get("track_wheels", [])]
              + state.get("track_pool", [])[:1])
    if (state.get("track_world") == world.get_path_name() and cached[0] is not None
            and all(valid(o) for o in cached)):
        return state
    state.update(track_world=world.get_path_name(), track_wheels=[], track_next=0, track_last={},
                 track_pending=[], track_time={})
    pool = track_pool(world)
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
        state["track_wheels"] = [(w, wheel_size(w)[0]) for w in rover_wheels(rover)]
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
    now = unreal.GameplayStatics.get_time_seconds(world)
    centres = [wheel.get_world_location() for wheel, _ in state["track_wheels"]]
    if now < SETTLE_TIME_S:             # Still settling: just follow the wheels
        for (wheel, _), centre in zip(state["track_wheels"], centres):
            state["track_last"][wheel.get_name()] = centre
            state["track_time"][wheel.get_name()] = now
        return
    pending = state["track_pending"]
    for (wheel, radius), centre in zip(state["track_wheels"], centres):
        key = wheel.get_name()
        last = state["track_last"].get(key)
        if last is None:
            state["track_last"][key], state["track_time"][key] = centre, now
            continue
        travel = unreal.Vector(centre.x - last.x, centre.y - last.y, 0.0)
        distance = math.hypot(travel.x, travel.y)
        if distance < TRACK_SPACING_CM:
            continue
        direction = unreal.Vector(travel.x / distance, travel.y / distance, 0.0)
        state["track_time"][key] = now
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
