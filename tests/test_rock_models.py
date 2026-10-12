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
