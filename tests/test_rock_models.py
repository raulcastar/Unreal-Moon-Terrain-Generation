"""rock_models: grouping rocks into instanced components, and the send command."""

import rock_models
import send_rocks


def test_instance_groups_one_per_mesh_and_stencil():
    rocks = [("a", 9, 1), ("b", 9, 2), ("a", 9, 3), ("a", 8, 4), ("b", 9, 5)]
    groups = rock_models.instance_groups(rocks)
    assert groups == [(("a", 9), [1, 3]), (("b", 9), [2, 5]), (("a", 8), [4])]


def test_instance_groups_keeps_every_rock_once():
    rocks = [(f"m{i % 7}", 9 if i % 3 else 10, i) for i in range(1000)]
    groups = rock_models.instance_groups(rocks)
    assert sorted(item for _key, items in groups for item in items) == list(range(1000))
    assert len(groups) == 14


def test_instance_groups_stencil_ids_are_ints():
    assert rock_models.instance_groups([("a", 9.0, 1)]) == [(("a", 9), [1])]


def test_instance_command_calls_the_converter():
    command = send_rocks.build_instance_command("procedural_lunar_terrain_1_2", save_level=False)
    assert "instance_rocks('procedural_lunar_terrain_1_2', save_level=False)" in command


def test_texture_for_slot_matches_poly_haven_names():
    """A glTF slot left empty on import is filled with the model's texture of that kind."""
    assets = ["moon_rock_01_diff", "moon_rock_01_nor_gl", "moon_rock_01_rough"]      # as Unreal names them
    assert rock_models.texture_for_slot("BaseColorTexture", assets) == "moon_rock_01_diff"
    assert rock_models.texture_for_slot("MetallicRoughnessTexture", assets) == "moon_rock_01_rough"
    assert rock_models.texture_for_slot("NormalTexture", assets) == "moon_rock_01_nor_gl"
    files = ["moon_rock_01_arm_1k.jpg", "moon_rock_01_diff_1k.jpg", "moon_rock_01_nor_gl_1k.jpg"]
    assert rock_models.texture_for_slot("BaseColorTexture", files) == "moon_rock_01_diff_1k.jpg"
    assert rock_models.texture_for_slot("MetallicRoughnessTexture", files) == "moon_rock_01_arm_1k.jpg"
    assert rock_models.texture_for_slot("NormalTexture", files) == "moon_rock_01_nor_gl_1k.jpg"
    assert rock_models.texture_for_slot("BaseColorTexture", ["rock_normal"]) is None
    assert rock_models.texture_for_slot("EmissiveTexture", assets) is None
