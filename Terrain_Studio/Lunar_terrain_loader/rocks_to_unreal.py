"""
Rock Field Dispatch
-------------------
Sends a planned rock field (<name>_rocks.csv) to a running Unreal Editor over
Remote Execution, where unreal_rock_placer imports the rock models and places
the rocks on the Landscape.
"""

import json
import os
import time

import unreal_api

BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")


def heightmap_width(output_directory, dem_stem):
    """Landscape width in vertices from <dem_stem>_heightmap.json, or 0 if absent."""
    path = os.path.join(output_directory, f"{dem_stem}_heightmap.json")
    try:
        with open(path) as handle:
            return int(json.load(handle).get("width", 0))
    except (OSError, ValueError):
        return 0


def _placer_call(call):
    """Python that imports the placer inside Unreal and prints the result of `call`."""
    return (
        f"import sys, importlib\n"
        f"if {BASE_DIRECTORY!r} not in sys.path: sys.path.append({BASE_DIRECTORY!r})\n"
        f"import unreal_level_saver\n"
        f"importlib.reload(unreal_level_saver)\n"
        f"import unreal_rock_placer\n"
        f"importlib.reload(unreal_rock_placer)\n"
        f"print(unreal_rock_placer.{call})\n"
    )


def build_command(rocks_csv, models_folder, resolution, landscape_label,
                  landscape_width, seed, simulate_physics,
                  boulder_stencil=20, small_rock_stencil=21, boulder_min_radius_m=0.15,
                  save_level=True):
    """The Python Unreal runs to place the rocks. Paths are passed with repr() quoting."""
    args = ", ".join([
        repr(rocks_csv.replace("\\", "/")),
        repr(models_folder.replace("\\", "/")),
        f"resolution={float(resolution)!r}",
        f"landscape_label={landscape_label!r}",
        f"landscape_width={int(landscape_width)}",
        f"seed={int(seed)}",
        f"simulate_physics={bool(simulate_physics)}",
        f"boulder_stencil={int(boulder_stencil)}",
        f"small_rock_stencil={int(small_rock_stencil)}",
        f"boulder_min_radius_m={float(boulder_min_radius_m)!r}",
        f"save_level={bool(save_level)}",
    ])
    return _placer_call(f"place_rocks({args})")


def build_stencil_command(group, boulder_stencil, small_rock_stencil, boulder_min_radius_m,
                          save_level=True):
    """The Python Unreal runs to re-tag placed rocks without moving them."""
    args = ", ".join([repr(group), f"boulder_stencil={int(boulder_stencil)}",
                      f"small_rock_stencil={int(small_rock_stencil)}",
                      f"boulder_min_radius_m={float(boulder_min_radius_m)!r}",
                      f"save_level={bool(save_level)}"])
    return _placer_call(f"set_rock_stencils({args})")


def run_in_unreal(command, log=print, action="Running in Unreal",
                  discovery_seconds=2.0):
    """
    Runs `command` in the first Unreal Editor that answers and relays its output
    to `log`. Blocks until Unreal finishes. Returns True on success.
    """
    remote_exec = unreal_api.RemoteExecution()
    remote_exec.start()
    try:
        log("Searching for a running Unreal Editor...")
        time.sleep(discovery_seconds)
        nodes = remote_exec.remote_nodes
        if not nodes:
            log("[-] No Unreal instance detected. Enable the Python Editor Script "
                "Plugin and 'Remote Execution' in Project Settings.", "error")
            return False

        node = nodes[0]
        node_id = node.get("node_id") if isinstance(node, dict) else getattr(node, "node_id", None)
        remote_exec.open_command_connection(node_id)
        log(f"{action} (node {node_id})...")
        result = remote_exec.run_command(command)
    finally:
        remote_exec.stop()

    for entry in result.get("output", []):
        text = str(entry.get("output", "")).strip()
        if text:
            level = {"Warning": "warn", "Error": "error"}.get(entry.get("type"))
            log(text, level)
    if not result.get("success", False):
        log(f"[-] Failed in Unreal: {result.get('result', '')}", "error")
        return False
    return True


def send_rocks_to_unreal(rocks_csv, models_folder, resolution, landscape_label="",
                         landscape_width=0, seed=1, simulate_physics=False,
                         boulder_stencil=20, small_rock_stencil=21,
                         boulder_min_radius_m=0.15, save_level=True, log=print,
                         discovery_seconds=2.0):
    """
    Places the rock field in a running Unreal Editor, then saves the level
    when `save_level`. Returns True on success.
    """
    command = build_command(rocks_csv, models_folder, resolution, landscape_label,
                            landscape_width, seed, simulate_physics,
                            boulder_stencil, small_rock_stencil, boulder_min_radius_m,
                            save_level)
    return run_in_unreal(command, log, "Placing rocks in Unreal; this can take a while",
                         discovery_seconds)


def send_stencils_to_unreal(group, boulder_stencil=20, small_rock_stencil=21,
                            boulder_min_radius_m=0.15, save_level=True, log=print,
                            discovery_seconds=2.0):
    """
    Re-tags the placed rocks of `group` with new stencil ids, then saves the
    level when `save_level`. Returns True on success.
    """
    command = build_stencil_command(group, boulder_stencil, small_rock_stencil,
                                    boulder_min_radius_m, save_level)
    return run_in_unreal(command, log, "Updating rock stencils in Unreal",
                         discovery_seconds)
