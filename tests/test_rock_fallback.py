"""
unreal_commands/unreal_rocks.full_detail_fallback against a fake `unreal`
module: imported Nanite rocks get a full-detail fallback (the automatic one
self-shadows in black blotches under ray-traced shadows), once. No Unreal
Editor is reached.
"""
import importlib
import os
import sys
import types

import pytest

STUDIO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Terrain_Studio")


class Settings:
    def __init__(self, enabled=True):
        self.props = {"enabled": enabled, "fallback_target": "AUTO", "fallback_relative_error": 1.0}

    def get_editor_property(self, name):
        return self.props[name]

    def set_editor_property(self, name, value):
        self.props[name] = value


class Mesh:
    def __init__(self, enabled=True):
        self.settings, self.applied = Settings(enabled), 0

    def get_editor_property(self, name):
        assert name == "nanite_settings"
        return self.settings

    def set_editor_property(self, name, value):
        assert name == "nanite_settings"
        self.settings, self.applied = value, self.applied + 1


@pytest.fixture
def rocks(monkeypatch):
    u = types.ModuleType("unreal")
    u.saved = []
    u.NaniteFallbackTarget = types.SimpleNamespace(AUTO="AUTO", RELATIVE_ERROR="RELATIVE_ERROR")
    u.EditorAssetLibrary = types.SimpleNamespace(save_loaded_asset=lambda m: u.saved.append(m))
    monkeypatch.setitem(sys.modules, "unreal", u)
    monkeypatch.syspath_prepend(os.path.join(STUDIO, "unreal_commands"))
    for name in ("unreal_rocks", "unreal_level_saver"):
        sys.modules.pop(name, None)
    yield importlib.import_module("unreal_rocks"), u
    for name in ("unreal_rocks", "unreal_level_saver"):
        sys.modules.pop(name, None)


def test_a_nanite_rock_gets_a_full_detail_fallback_once(rocks):
    module, u = rocks
    mesh = Mesh()
    assert module.full_detail_fallback(mesh) is True
    assert mesh.settings.props["fallback_target"] == "RELATIVE_ERROR"
    assert mesh.settings.props["fallback_relative_error"] == 0.0
    assert u.saved == [mesh]
    assert module.full_detail_fallback(mesh) is False        # already set: no rebuild, no save
    assert mesh.applied == 1 and u.saved == [mesh]


def test_a_mesh_without_nanite_is_left_alone(rocks):
    module, u = rocks
    mesh = Mesh(enabled=False)
    assert module.full_detail_fallback(mesh) is False
    assert mesh.applied == 0 and u.saved == []
