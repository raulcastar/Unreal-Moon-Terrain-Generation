"""
The Landscape material fallback (unreal_commands/unreal_landscape.py), against a
fake `unreal` module: a project without /Game/Landscape gets the
UnrealDEMLandscape plugin's regolith material copied in, pointing at its own
copied textures. No Unreal Editor is reached.
"""
import copy
import importlib
import os
import sys
import types

import pytest

STUDIO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Terrain_Studio")
PLUGIN, GAME = "/UnrealDEMLandscape/Landscape", "/Game/Landscape"
TEXTURES = ("Assets/moon_01_4k/Textures/moon_01_diff", "Assets/moon_01_4k/Textures/moon_01_nor_gl")


class Texture:
    def __init__(self, path):
        self.path = path

    def get_path_name(self):
        return f"{self.path}.{self.path.rsplit('/', 1)[1]}"


class Node:
    def __init__(self, name, texture=None, inputs=()):
        self.name, self.texture, self.inputs = name, texture, list(inputs)

    def get_path_name(self):
        return self.name

    def get_editor_property(self, prop):
        if prop != "texture" or self.texture is None:
            raise AttributeError(prop)
        return self.texture

    def set_editor_property(self, prop, value):
        assert prop == "texture"
        self.texture = value


class Material:
    def __init__(self, path, root):
        self.path, self.root = path, root


class FakeUnreal(types.ModuleType):
    """Just what unreal_landscape's material code calls."""

    def __init__(self, assets):
        super().__init__("unreal")
        self.assets, self.saved, self.recompiled, self.logs = assets, [], [], []
        fake = self

        class EditorAssetLibrary:
            @staticmethod
            def does_asset_exist(path):
                return path in fake.assets

            @staticmethod
            def list_assets(folder, recursive=True, include_folder=False):
                return [f"{p}.{p.rsplit('/', 1)[1]}" for p in fake.assets if p.startswith(folder + "/")]

            @staticmethod
            def duplicate_asset(source, target):
                obj = copy.deepcopy(fake.assets[source])
                if isinstance(obj, Texture):
                    obj.path = target
                else:
                    obj.path = target
                    # As in Unreal: the copy still references the source's textures.
                    for node in _walk(obj.root):
                        if node.texture is not None:
                            node.texture = fake.assets[node.texture.path]
                fake.assets[target] = obj
                return obj

            @staticmethod
            def save_asset(path, only_if_is_dirty=True):
                fake.saved.append(path)
                return True

        class MaterialEditingLibrary:
            @staticmethod
            def get_material_property_input_node(material, prop):
                return material.root if prop == "BASE_COLOR" else None

            @staticmethod
            def get_inputs_for_material_expression(material, node):
                return node.inputs

            @staticmethod
            def recompile_material(material):
                fake.recompiled.append(material.path)

        self.EditorAssetLibrary = EditorAssetLibrary
        self.MaterialEditingLibrary = MaterialEditingLibrary
        self.MaterialProperty = ["BASE_COLOR", "NORMAL", "ROUGHNESS"]
        self.load_asset = lambda path: fake.assets.get(path)
        self.log = lambda msg: fake.logs.append(msg)
        self.log_warning = lambda msg: fake.logs.append("WARNING " + msg)


def _walk(root):
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(n.inputs)
    return out


def plugin_assets():
    tex = {t: Texture(f"{PLUGIN}/{t}") for t in TEXTURES}
    # A shared texture node reached twice, as in a near/far blend.
    diff = Node("diff", tex[TEXTURES[0]])
    root = Node("lerp", inputs=[diff, Node("desat", inputs=[diff]), Node("normal", tex[TEXTURES[1]]),
                                Node("const")])
    assets = {f"{PLUGIN}/{t}": tex[t] for t in TEXTURES}
    assets[f"{PLUGIN}/Landscape_Material"] = Material(f"{PLUGIN}/Landscape_Material", root)
    return assets


@pytest.fixture
def landscape(monkeypatch):
    def load(assets):
        fake = FakeUnreal(assets)
        monkeypatch.setitem(sys.modules, "unreal", fake)
        monkeypatch.syspath_prepend(os.path.join(STUDIO, "unreal_commands"))
        for name in ("unreal_landscape", "unreal_level_saver"):
            sys.modules.pop(name, None)
        module = importlib.import_module("unreal_landscape")
        return module, fake
    yield load
    for name in ("unreal_landscape", "unreal_level_saver"):
        sys.modules.pop(name, None)


def test_a_project_without_the_material_gets_the_plugins_copy(landscape):
    module, fake = landscape(plugin_assets())
    material = module.landscape_material()
    assert material is fake.assets[f"{GAME}/Landscape_Material"]
    textures = {n.texture.path for n in _walk(material.root) if n.texture is not None}
    assert textures == {f"{GAME}/{t}" for t in TEXTURES}         # its own copies
    plugin = fake.assets[f"{PLUGIN}/Landscape_Material"]
    assert {n.texture.path for n in _walk(plugin.root) if n.texture} == {f"{PLUGIN}/{t}" for t in TEXTURES}
    assert sorted(fake.saved) == sorted([f"{GAME}/Landscape_Material"] + [f"{GAME}/{t}" for t in TEXTURES])
    assert fake.recompiled == [f"{GAME}/Landscape_Material"]
    assert any("copied the lunar regolith material" in m for m in fake.logs)


def test_the_projects_own_material_is_used_untouched(landscape):
    assets = plugin_assets()
    own = Material(f"{GAME}/Landscape_Material", Node("mare"))
    assets[f"{GAME}/Landscape_Material"] = own
    module, fake = landscape(assets)
    assert module.landscape_material() is own
    assert fake.saved == [] and fake.recompiled == []


def test_without_the_plugin_or_for_another_material_nothing_is_copied(landscape):
    module, fake = landscape({})
    assert module.landscape_material() is None
    module, fake = landscape(plugin_assets())
    assert module.landscape_material("/Game/Other/M_Ground") is None
    assert fake.saved == []
