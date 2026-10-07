"""
Unreal Wheel Dust (Terrain Studio runtime)
------------------------------------------
Executes natively within the Unreal Engine Python environment. Drives the
Niagara wheel dust (/Game/Landscape/NS_LunarDust) during Play In Editor; called
every frame by the editor watcher (unreal_watcher).

Each wheel gets its own emitter (NiagaraComponents spawned in the running game,
inactive, never attached to the rover, which is only read). An emitter is
activated the first time its wheel rolls on the ground after the rover has
settled (SETTLE_TIME_S), and then stays running for the rest of the Play: switching a Niagara system off and on ends and
restarts it, emptying it each time. Every frame each emitter is kept at its
wheel's ground contact, oriented with local +X pointing backwards along the
wheel's travel and +Z up, and gets two float user parameters: WheelSpeed, the
wheel's speed (cm/s, smoothed), and DustAmount, the studio's Dust amount. Both
are 0 while the wheel stands still or is off the ground (no track piece for
CONTACT_TIMEOUT_S), so the system's Spawn Rate must be multiplied by
User.DustAmount. The studio's setup (Lunar_terrain_loader/unreal_dust_setup.py)
prepares the sprite material and the system's mask stencil.
"""

import math

import unreal

from unreal_wheel_tracks import SETTLE_TIME_S

DUST_SYSTEM = "/Game/Landscape/NS_LunarDust"
SPEED_PARAMETER = "WheelSpeed"           # Niagara user parameter (float, cm/s)
AMOUNT_PARAMETER = "DustAmount"          # Niagara user parameter (float, 1 = default; studio slider)
CONTACT_TIMEOUT_S = 1.5                  # A wheel counts as on the ground this long after its last track piece
MIN_SPEED_CMS = 2.0                      # Slower than this is standing still
SPEED_SMOOTHING_S = 0.3                  # Time constant of the smoothed wheel speed


def _alive(obj):
    """True if `obj` still exists. (is_valid raises, not returns False, for an object
    of an earlier Play session.)"""
    try:
        return obj is not None and unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


def kick(state, world, now, ground, direction, speed_cms):
    """A wheel rolled on by one track piece: remember it, so its emitter runs."""
    wheel = state.get("dust_current_wheel")
    if wheel is None:
        return
    state.setdefault("dust_rolling", {})[wheel] = (now, direction, speed_cms)


def _emitters(world, state):
    """One emitter per wheel of `world` (a running game), created once and checked."""
    wheels = state.get("track_wheels") or []
    cached = state.get("dust_emitters")
    if (cached is not None and state.get("dust_world") == world.get_path_name()
            and len(cached) == len(wheels) and all(_alive(e) for e in cached.values())):
        return cached
    system = unreal.load_asset(DUST_SYSTEM)
    emitters = {}
    if system is not None:
        for wheel, _ in wheels:
            emitter = unreal.NiagaraFunctionLibrary.spawn_system_at_location(
                world, system, wheel.get_world_location(), unreal.Rotator(0.0, 0.0, 0.0),
                unreal.Vector(1.0, 1.0, 1.0), False, False, unreal.NCPoolMethod.NONE, True)
            emitters[wheel.get_name()] = emitter
    state.update(dust_emitters=emitters, dust_world=world.get_path_name(), dust_rolling={},
                 dust_motion={})
    return emitters


def update_wheel_dust(world, state):
    """Called every frame by the editor watcher while a game runs (after the tracks)."""
    if world is None or not state.get("dust_on", True) or not state.get("track_wheels"):
        return
    emitters = _emitters(world, state)
    if not emitters:
        return
    now = unreal.GameplayStatics.get_time_seconds(world)
    rolling = state.get("dust_rolling", {})
    motion = state.setdefault("dust_motion", {})      # wheel -> (time, x, y, smoothed speed)
    amount = float(state.get("dust_amount", 1.0))
    for wheel, radius in state["track_wheels"]:
        key = wheel.get_name()
        emitter = emitters.get(key)
        if emitter is None:
            continue
        centre = wheel.get_world_location()
        last_time, last_x, last_y, speed = motion.get(key, (now, centre.x, centre.y, 0.0))
        dt = now - last_time
        if dt > 1e-3:
            measured = math.hypot(centre.x - last_x, centre.y - last_y) / dt
            blend = min(1.0, dt / SPEED_SMOOTHING_S)
            speed += (measured - speed) * blend
        motion[key] = (now, centre.x, centre.y, speed)

        piece = rolling.get(key)                       # (time, direction, speed) of its last track piece
        on_ground = piece is not None and now - piece[0] < CONTACT_TIMEOUT_S
        kicking = on_ground and speed > MIN_SPEED_CMS and now >= SETTLE_TIME_S
        # Always at the wheel's lowest point (where it meets the ground once landed),
        # pointing backwards along its last travel.
        ground = unreal.Vector(centre.x, centre.y, centre.z - radius)
        if piece is not None:
            back = unreal.Vector(-piece[1].x, -piece[1].y, 0.0)
            emitter.set_world_location_and_rotation(
                ground, unreal.MathLibrary.make_rot_from_xz(back, unreal.Vector(0.0, 0.0, 1.0)),
                False, False)
        else:
            emitter.set_world_location(ground, False, False)
        emitter.set_variable_float(SPEED_PARAMETER, float(speed) if kicking else 0.0)
        emitter.set_variable_float(AMOUNT_PARAMETER, amount if kicking else 0.0)
        # Started the first time its wheel really rolls on the ground (never while
        # the rover drops and settles), then left running: switching a Niagara
        # system off and on restarts it empty.
        if kicking and not emitter.is_active():
            emitter.activate(False)
