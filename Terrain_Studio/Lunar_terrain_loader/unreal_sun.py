"""
Unreal Lunar Sun
----------------
Executes natively within the Unreal Engine Python environment. Points a
Directional Light at the sun chosen in the studio, with lunar settings: white
light, a 0.53 degree solar disc and long dynamic shadows. The light stays the
SkyAtmosphere's sun (Atmosphere Sun Light, index 0), so the sky draws its disc.

Azimuth is clockwise from the top of the DEM image: 0 = -Y, 90 = -X. DEM
columns run towards -X and rows towards +Y (unreal_rock_placer.landscape_frame).
"""

import json
import math

import unreal

SUN_JSON_PREFIX = "SUN_JSON "
SUN_LABEL = "LunarSun"
SUN_TAG = "LunarSun"
SUN_DISC_DEG = 0.53           # Angular diameter of the Sun seen from the Moon
SHADOW_DISTANCE_CM = 20000.0  # Minimum dynamic shadow distance (200 m)
SUN_HEIGHT_CM = 50000.0       # Spawn height; position does not affect a directional light


def light_rotation(azimuth_deg, elevation_deg):
    """Rotator whose forward vector points from the sun towards the ground."""
    a = math.radians(azimuth_deg)
    yaw = math.degrees(math.atan2(math.cos(a), math.sin(a)))
    return unreal.Rotator(roll=0.0, pitch=-float(elevation_deg), yaw=yaw)


def sun_angles(light):
    """(azimuth, elevation) in degrees of the sun a Directional Light represents."""
    forward = light.get_actor_forward_vector()
    elevation = math.degrees(math.asin(max(-1.0, min(1.0, -forward.z))))
    azimuth = math.degrees(math.atan2(forward.x, forward.y)) % 360.0
    return azimuth, elevation


def _in_pie():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).is_in_play_in_editor()


def _current_world():
    editor = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    return editor.get_game_world() if _in_pie() else editor.get_editor_world()


def _directional_lights(world):
    return list(unreal.GameplayStatics.get_all_actors_of_class(world, unreal.DirectionalLight))


def _pick_light(lights):
    """The studio's sun if present, else the level's only directional light."""
    for light in lights:
        if light.get_actor_label() == SUN_LABEL or SUN_TAG in [str(t) for t in light.tags]:
            return light
    return lights[0] if len(lights) == 1 else None


def _spawn_sun():
    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    light = actors.spawn_actor_from_class(unreal.DirectionalLight,
                                          unreal.Vector(0.0, 0.0, SUN_HEIGHT_CM))
    light.set_actor_label(SUN_LABEL)
    light.tags = [SUN_TAG]
    return light


def _apply_editor(light, rotation, intensity_lux, source_angle_deg):
    """Sets the light through editor properties, so the change is saved with the level."""
    light.set_actor_rotation(rotation, False)
    comp = light.light_component
    comp.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)
    comp.set_editor_property("intensity", float(intensity_lux))
    comp.set_editor_property("light_color", unreal.Color(255, 255, 255, 255))
    comp.set_editor_property("use_temperature", False)
    comp.set_editor_property("light_source_angle", float(source_angle_deg))
    comp.set_editor_property("light_source_soft_angle", 0.0)
    comp.set_editor_property("atmosphere_sun_light", True)      # Linked to the SkyAtmosphere
    comp.set_editor_property("atmosphere_sun_light_index", 0)
    comp.set_editor_property("cast_shadows", True)
    distance = comp.get_editor_property("dynamic_shadow_distance_movable_light")
    comp.set_editor_property("dynamic_shadow_distance_movable_light",
                             max(float(distance), SHADOW_DISTANCE_CM))


def _apply_runtime(light, rotation, intensity_lux, source_angle_deg):
    """Sets a running game's light through its runtime setters, which apply at once."""
    light.set_actor_rotation(rotation, False)
    comp = light.light_component
    comp.set_intensity(float(intensity_lux))
    comp.set_light_color(unreal.LinearColor(1.0, 1.0, 1.0, 1.0), True)
    comp.set_use_temperature(False)
    comp.set_light_source_angle(float(source_angle_deg))
    comp.set_light_source_soft_angle(0.0)
    comp.set_atmosphere_sun_light(True)
    comp.set_atmosphere_sun_light_index(0)
    comp.set_cast_shadows(True)
    distance = comp.get_editor_property("dynamic_shadow_distance_movable_light")
    comp.set_dynamic_shadow_distance_movable_light(max(float(distance), SHADOW_DISTANCE_CM))


def set_sun(azimuth_deg, elevation_deg, intensity_lux=120000.0,
            source_angle_deg=SUN_DISC_DEG, save_level=False):
    """
    Points the level's sun at (azimuth, elevation) with lunar settings. Uses the
    'LunarSun' light or the level's only Directional Light, else spawns one.
    During Play In Editor only the running game changes. Returns a summary line.
    """
    rotation = light_rotation(azimuth_deg, elevation_deg)
    sun = f"azimuth {azimuth_deg:.1f} deg, elevation {elevation_deg:.1f} deg"

    if _in_pie():
        world = _current_world()
        light = _pick_light(_directional_lights(world)) if world else None
        if light is None:
            return ("Play In Editor is running and its world has no single Directional "
                    "Light to move; stop it and send the sun again.")
        _apply_runtime(light, rotation, intensity_lux, source_angle_deg)
        unreal.log_warning("Play In Editor is running: the sun was changed in the running "
                           "game only. Stop it and send the sun again to keep it in the level.")
        return f"Sun set in the running game on '{light.get_actor_label()}': {sun}."

    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        return "No level is open."
    lights = _directional_lights(world)
    light = _pick_light(lights)
    action = "Updated"
    if light is None:
        if lights:
            unreal.log_warning(f"{len(lights)} Directional Lights in the level; spawning "
                               f"'{SUN_LABEL}'. Remove the others to avoid double shadows.")
        light, action = _spawn_sun(), "Spawned"
    _apply_editor(light, rotation, intensity_lux, source_angle_deg)

    summary = f"{action} '{light.get_actor_label()}': {sun}, {intensity_lux:.0f} lux."
    if save_level:
        import unreal_level_saver
        summary += " " + unreal_level_saver.save_current_level()
    return summary


def get_sun():
    """
    Describes the level's sun (the running game's during Play In Editor) as
    SUN_JSON_PREFIX + JSON: label, azimuth, elevation, intensity, source_angle, pie.
    """
    world = _current_world()
    light = _pick_light(_directional_lights(world)) if world else None
    if light is None:
        return SUN_JSON_PREFIX + json.dumps({"error": "no single Directional Light in the level"})
    azimuth, elevation = sun_angles(light)
    comp = light.light_component
    return SUN_JSON_PREFIX + json.dumps({
        "label": light.get_actor_label(), "azimuth": azimuth, "elevation": elevation,
        "intensity": float(comp.get_editor_property("intensity")),
        "source_angle": float(comp.get_editor_property("light_source_angle")),
        "pie": _in_pie()})
