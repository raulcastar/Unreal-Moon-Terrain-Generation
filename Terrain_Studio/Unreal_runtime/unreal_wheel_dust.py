"""
Unreal Wheel Dust
-----------------
Executes natively within the Unreal Engine Python environment. Kicks up regolith
behind the rover's wheels during Play In Editor with a Niagara system
(/Game/Landscape/NS_LunarDust).

Each wheel gets its own emitter (NiagaraComponents spawned in the running game,
never attached to the rover, which is only read). They are activated once and
stay running for the whole Play: switching a Niagara system off and on ends and
restarts it, emptying it each time. Every frame the editor watcher keeps each
emitter at its wheel's ground contact, oriented with local +X pointing
backwards along the wheel's travel and +Z up, and sets two float user
parameters: WheelSpeed, the wheel's speed (cm/s, smoothed), and DustAmount, the
studio's Dust amount. Both are 0 while the wheel stands still or is off the
ground (no track piece for CONTACT_TIMEOUT_S), so the system's Spawn Rate must
be multiplied by User.DustAmount (and may scale with User.WheelSpeed); grains
already in the air finish their arcs.

The particle look and physics live in the Niagara system. For lunar dust (no
air): Gravity Force (0, 0, -162) cm/s^2, no Drag, launch velocity backwards and
up, about one second of life, and the soft sprite M_LunarDustSprite made here.
The system writes CustomDepth stencil DUST_ID, so the object mask labels the
dust.
"""

import math

import unreal

GROUND_TRUTH_FOLDER = "/Game/GroundTruth_Masks"
DUST_SYSTEM = "/Game/Landscape/NS_LunarDust"
DUST_SPRITE_MATERIAL = f"{GROUND_TRUTH_FOLDER}/M_LunarDustSprite"
OLD_DUST_LABEL = "WheelDust"             # The earlier instanced-grain version: removed
OLD_GRAIN_MATERIAL = f"{GROUND_TRUTH_FOLDER}/M_LunarDustGrain"
DUST_ID = 70
SPEED_PARAMETER = "WheelSpeed"           # Niagara user parameter (float, cm/s)
AMOUNT_PARAMETER = "DustAmount"          # Niagara user parameter (float, 1 = default; studio slider)
CONTACT_TIMEOUT_S = 1.5                  # A wheel counts as on the ground this long after its last track piece
MIN_SPEED_CMS = 2.0                      # Slower than this is standing still
SPEED_SMOOTHING_S = 0.3                  # Time constant of the smoothed wheel speed
REGOLITH_COLOUR = unreal.LinearColor(0.13, 0.128, 0.124, 1.0)
SPRITE_STRENGTH = 0.95                   # Sprite opacity at its centre (times the particle's alpha)

SPRITE_OPACITY_CODE = r"""// Soft round puff: opaque-ish centre fading to a clear edge, times the
// particle's own alpha (Niagara colour / fade over life).
float r = length(UV - 0.5) * 2.0;
return saturate(1.0 - r * r) * Strength * Alpha;"""


def _alive(obj):
    """True if `obj` still exists. (is_valid raises, not returns False, for an object
    of an earlier Play session.)"""
    try:
        return obj is not None and unreal.SystemLibrary.is_valid(obj)
    except Exception:
        return False


def _sprite_material():
    """Soft, round, translucent regolith-coloured sprite for the dust particles."""
    lib = unreal.MaterialEditingLibrary
    material = unreal.load_asset(DUST_SPRITE_MATERIAL)
    if material is None:
        material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            DUST_SPRITE_MATERIAL.rsplit("/", 1)[-1], GROUND_TRUTH_FOLDER, unreal.Material,
            unreal.MaterialFactoryNew())
    lib.delete_all_material_expressions(material)
    material.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
    material.set_editor_property("translucency_lighting_mode",
                                 unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)   # Lit by the sun directly
    material.set_editor_property("allow_translucent_custom_depth_writes", True)   # Object mask
    material.set_editor_property("used_with_niagara_sprites", True)

    particle = lib.create_material_expression(material, unreal.MaterialExpressionParticleColor, -700, 0)
    colour = lib.create_material_expression(material, unreal.MaterialExpressionConstant3Vector, -700, -120)
    colour.set_editor_property("constant", REGOLITH_COLOUR)
    tint = lib.create_material_expression(material, unreal.MaterialExpressionMultiply, -400, -80)
    lib.connect_material_expressions(colour, "", tint, "A")
    lib.connect_material_expressions(particle, "RGB", tint, "B")
    lib.connect_material_property(tint, "", unreal.MaterialProperty.MP_BASE_COLOR)

    uv = lib.create_material_expression(material, unreal.MaterialExpressionTextureCoordinate, -700, 200)
    strength = lib.create_material_expression(material, unreal.MaterialExpressionScalarParameter, -700, 280)
    strength.set_editor_property("parameter_name", "Strength")
    strength.set_editor_property("default_value", SPRITE_STRENGTH)
    opacity = lib.create_material_expression(material, unreal.MaterialExpressionCustom, -400, 200)
    opacity.set_editor_property("code", SPRITE_OPACITY_CODE)
    opacity.set_editor_property("output_type", unreal.CustomMaterialOutputType.CMOT_FLOAT1)
    entries = []
    for name in ("UV", "Strength", "Alpha"):
        entry = unreal.CustomInput()
        entry.set_editor_property("input_name", name)
        entries.append(entry)
    opacity.set_editor_property("inputs", entries)
    lib.connect_material_expressions(uv, "", opacity, "UV")
    lib.connect_material_expressions(strength, "", opacity, "Strength")
    lib.connect_material_expressions(particle, "A", opacity, "Alpha")
    lib.connect_material_property(opacity, "", unreal.MaterialProperty.MP_OPACITY)
    lib.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def remove_old_dust(world=None):
    """Removes the earlier instanced-grain dust (actor and material). Returns how many actors."""
    world = world or unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    actors = [a for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)
              if a.get_actor_label().endswith("_" + OLD_DUST_LABEL)]
    for actor in actors:
        actor.destroy_actor()
    if unreal.EditorAssetLibrary.does_asset_exist(OLD_GRAIN_MATERIAL):
        unreal.EditorAssetLibrary.delete_asset(OLD_GRAIN_MATERIAL)
    return len(actors)


def remove_wheel_dust(world=None):
    """Nothing to remove from the level any more (emitters live only in the game); clears the old version."""
    return remove_old_dust(world)


def setup_wheel_dust(rover):
    """
    Readies the dust for `rover`'s wheels: the sprite material, and the system
    writing the dust stencil. Editor only. Returns (ready, summary line).
    """
    remove_old_dust()
    system = unreal.load_asset(DUST_SYSTEM)
    if system is None:
        return False, f"Wheel dust: off, {DUST_SYSTEM} not found."
    _sprite_material()
    system.set_editor_property("render_custom_depth", True)
    system.set_editor_property("custom_depth_stencil_value", DUST_ID)
    unreal.EditorAssetLibrary.save_loaded_asset(system)
    return True, (f"Wheel dust: {DUST_SYSTEM.rsplit('/', 1)[-1]} at each wheel of "
                  f"{rover.get_actor_label()} while it rolls (dust is id {DUST_ID} in the object "
                  f"mask); sprite material {DUST_SPRITE_MATERIAL}.")


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
        kicking = on_ground and speed > MIN_SPEED_CMS
        if piece is not None:
            back = unreal.Vector(-piece[1].x, -piece[1].y, 0.0)
            emitter.set_world_location_and_rotation(
                unreal.Vector(centre.x, centre.y, centre.z - radius),
                unreal.MathLibrary.make_rot_from_xz(back, unreal.Vector(0.0, 0.0, 1.0)), False, False)
        emitter.set_variable_float(SPEED_PARAMETER, float(speed) if kicking else 0.0)
        emitter.set_variable_float(AMOUNT_PARAMETER, amount if kicking else 0.0)
        if not emitter.is_active():
            emitter.activate(False)                     # Once; it then stays running
