"""
The Terrain Studio MCP server (mcp/terrain_studio_mcp.py). Unreal is a stub
(every sender's run_in_unreal) and outputs go to the conftest's temporary
folder: nothing reaches an editor or the real outputs/.
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import terrain_studio_mcp as studio  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

SMALL = {"ROWS": 64, "COLS": 64}


class FakeUnreal:
    def __init__(self, playing=False, answers=True):
        self.state = {"level": "Lunar_Terrain_Yutu", "playing": playing,
                      "landscapes": [], "rovers": ["Yutu_Rover0"]}
        self.answers = answers
        self.commands = []

    def __call__(self, command, log=print, action="", discovery_seconds=2.0):
        self.commands.append(command)
        if not self.answers:
            log("[-] No Unreal instance detected.", "error")
            return False
        if studio.STATE_PREFIX in command:
            log(studio.STATE_PREFIX + json.dumps(self.state))
        if "editor_request_begin_play" in command:
            self.state["playing"] = True
        if "create_landscape" in command:
            log("Landscape built.")
        return True


@pytest.fixture()
def unreal(monkeypatch):
    fake = FakeUnreal()
    import send_masks, send_rocks, send_sun, send_terrain, unreal_connection
    for module in (unreal_connection, send_masks, send_rocks, send_sun, send_terrain):
        monkeypatch.setattr(module, "run_in_unreal", fake)
    return fake


def test_tools_are_listed_with_descriptions():
    tools = {t.name: t for t in asyncio.run(studio.server.list_tools())}
    for name in ("list_presets", "generate_terrain", "plan_rocks", "unreal_status", "unreal_play",
                 "send_terrain_to_unreal", "send_rocks_to_unreal", "set_sun"):
        assert tools[name].description
    assert tools["send_terrain_to_unreal"].annotations.destructive_hint


def test_generate_writes_to_outputs_and_refuses_to_overwrite(outputs_dir, unreal):
    result = studio.generate_terrain("mcp_test", seed=7, overrides=SMALL)
    assert result["size_px"] == [64, 64] and result["seed"] == 7
    assert os.path.isfile(outputs_dir / "mcp_test" / "mcp_test_elevationMap.npy")
    with pytest.raises(ToolError, match="already exists"):
        studio.generate_terrain("mcp_test", overrides=SMALL)
    assert studio.generate_terrain("mcp_test", seed=8, overrides=SMALL, overwrite=True)["seed"] == 8
    assert "outputs/mcp_test/mcp_test_elevationMap.npy" in studio.list_dems()["outputs"]
    assert unreal.commands == []                      # files only


def test_bad_parameters_say_what_is_valid(outputs_dir):
    with pytest.raises(ToolError, match="Unknown parameters"):
        studio.generate_terrain("x", overrides={"CRATERS": 3})
    with pytest.raises(ToolError, match="larger than one Unreal Landscape"):
        studio.generate_terrain("x", overrides={"ROWS": 5000, "COLS": 5000})
    with pytest.raises(ToolError, match="Unknown preset"):
        studio.generate_terrain("x", preset="mars")


def test_describe_and_plan_rocks(outputs_dir):
    studio.generate_terrain("rocky", seed=3, overrides=SMALL)
    info = studio.describe_dem("outputs/rocky/rocky_elevationMap.npy")
    assert info["size_px"] == [64, 64] and "slope_deg" in info
    plan = studio.plan_rocks("rocky/rocky_elevationMap.npy", seed=1)
    assert plan["group"] == "rocky" and os.path.isfile(outputs_dir / "rocky" / "rocky_rocks.csv")


def test_level_changes_are_refused_during_play(outputs_dir, unreal):
    studio.generate_terrain("t", seed=1, overrides=SMALL)
    unreal.state["playing"] = True
    with pytest.raises(ToolError, match="unreal_stop"):
        studio.send_terrain_to_unreal("t/t_elevationMap.npy")
    assert not any("create_landscape" in c for c in unreal.commands)


def test_send_terrain_builds_a_landscape(outputs_dir, unreal):
    studio.generate_terrain("t", seed=1, overrides=SMALL)
    result = studio.send_terrain_to_unreal("t/t_elevationMap.npy", save_level=False)
    assert os.path.isfile(result["heightmap"]) and str(outputs_dir) in result["heightmap"]
    assert any("create_landscape" in c and "save_level=False" in c for c in unreal.commands)


def test_rocks_need_their_landscape(outputs_dir, unreal, tmp_path):
    studio.generate_terrain("t", seed=1, overrides=SMALL)
    studio.plan_rocks("t/t_elevationMap.npy", seed=1)
    with pytest.raises(ToolError, match="send_terrain_to_unreal first"):
        studio.send_rocks_to_unreal("t/t_elevationMap.npy", str(tmp_path))


def test_play_and_an_absent_editor(unreal):
    assert studio.unreal_play()["playing"]
    assert any("editor_request_begin_play" in c for c in unreal.commands)
    unreal.answers = False
    with pytest.raises(ToolError, match="Remote Execution"):
        studio.unreal_status()
