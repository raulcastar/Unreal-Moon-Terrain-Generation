"""
unreal_commands/unreal_sun.set_sun against a fake `unreal` module: the light's
intensity is kept unless one is given (a project lit for its own exposure turns
white at lunar sunlight), and a sun the command spawns gets lunar sunlight.
No Unreal Editor is reached.
"""
import importlib
import os
import sys
import types

import pytest

STUDIO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Terrain_Studio")


class Component:
    def __init__(self, intensity):
        self.props = {"intensity": intensity, "dynamic_shadow_distance_movable_light": 40000.0}

    def set_editor_property(self, name, value):
        self.props[name] = value

    def get_editor_property(self, name):
        return self.props[name]


class Light:
    def __init__(self, label, intensity):
        self.label, self.tags = label, []
        self.light_component = Component(intensity)

    def get_actor_label(self):
        return self.label

    def set_actor_label(self, label):
        self.label = label

    def set_actor_rotation(self, rotation, teleport):
        self.rotation = rotation


def fake_unreal(lights):
    u = types.ModuleType("unreal")
    world = object()
    spawned = []

    class Subsystem:
        def is_in_play_in_editor(self):
            return False

        def get_editor_world(self):
            return world

        def spawn_actor_from_class(self, cls, location):
            light = Light("new", 10.0)          # the engine default for a new Directional Light
            spawned.append(light)
            lights.append(light)
            return light

    class GameplayStatics:
        @staticmethod
        def get_all_actors_of_class(w, cls):
            return list(lights) if cls is u.DirectionalLight else []

    u.LevelEditorSubsystem = u.UnrealEditorSubsystem = u.EditorActorSubsystem = Subsystem
    u.get_editor_subsystem = lambda cls: Subsystem()
    u.GameplayStatics = GameplayStatics
    u.DirectionalLight, u.SkyAtmosphere, u.SkyAtmosphereComponent = object(), object(), object()
    u.Rotator = lambda roll, pitch, yaw: (roll, pitch, yaw)
    u.Vector = lambda x, y, z: (x, y, z)
    u.Color = lambda *a: a
    u.ComponentMobility = types.SimpleNamespace(MOVABLE="movable")
    u.log_warning = lambda msg: None
    u.spawned = spawned
    return u


@pytest.fixture
def sun(monkeypatch):
    def load(lights):
        u = fake_unreal(lights)
        monkeypatch.setitem(sys.modules, "unreal", u)
        monkeypatch.syspath_prepend(os.path.join(STUDIO, "unreal_commands"))
        sys.modules.pop("unreal_sun", None)
        return importlib.import_module("unreal_sun"), u
    yield load
    sys.modules.pop("unreal_sun", None)


def test_the_levels_intensity_is_kept_by_default(sun):
    light = Light("DirectionalLight", 5.0)
    module, _ = sun([light])
    summary = module.set_sun(135.0, 5.0)
    assert light.light_component.props["intensity"] == 5.0
    assert light.rotation == (0.0, -5.0, pytest.approx(-45.0))
    assert "5 lux (kept)" in summary


def test_an_explicit_intensity_is_set(sun):
    light = Light("LunarSun", 5.0)
    module, _ = sun([light])
    assert "120000 lux." in module.set_sun(135.0, 5.0, intensity_lux=120000.0)
    assert light.light_component.props["intensity"] == 120000.0


def test_a_spawned_sun_gets_lunar_sunlight(sun):
    module, u = sun([])
    summary = module.set_sun(90.0, 10.0)
    assert u.spawned and u.spawned[0].light_component.props["intensity"] == module.LUNAR_SUNLIGHT_LUX
    assert summary.startswith("Spawned 'LunarSun'")
