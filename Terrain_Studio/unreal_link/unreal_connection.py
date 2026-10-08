"""
Unreal Connection
-----------------
How the studio's senders (send_terrain, send_rocks, send_masks, send_sun) talk
to a running Unreal Editor over Remote Execution.

unreal_call(call, *modules) builds the Python that runs inside Unreal: it puts
unreal_commands/ (the modules that run in the editor) and this folder (for
rock_models, which they share with the studio) on Unreal's module path,
imports and reloads `modules` so the latest code runs, and prints the result of
`call`. run_in_unreal(command) sends it to the first editor that answers,
together with the runtime sync (runtime_sync), and relays the editor's output.
"""

import os
import time

import remote_execution
import runtime_sync

UNREAL_LINK_DIR = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
UNREAL_COMMANDS_DIR = os.path.join(os.path.dirname(UNREAL_LINK_DIR), "unreal_commands").replace("\\", "/")


def unreal_call(call, *modules):
    """
    Unreal-side Python that imports (and reloads) `modules` from unreal_commands
    and prints the result of `call`, e.g. unreal_call("set_sun(...)", "unreal_sun").
    """
    lines = ["import sys, importlib"]
    for folder in (UNREAL_LINK_DIR, UNREAL_COMMANDS_DIR):
        lines.append(f"if {folder!r} not in sys.path: sys.path.insert(0, {folder!r})")
    for module in modules:
        lines.append(f"import {module}")
        lines.append(f"importlib.reload({module})")
    lines.append(f"print({modules[-1]}.{call})" if modules else f"print({call})")
    return "\n".join(lines) + "\n"


def run_in_unreal(command, log=print, action="Running in Unreal",
                  discovery_seconds=2.0):
    """
    Runs `command` in the first responding Unreal Editor, relaying its output to
    `log`. Blocks until completion; returns True on success.
    """
    remote_exec = remote_execution.RemoteExecution()
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
        # Also refreshes the project's own copy of the editor watcher (runtime_sync).
        result = remote_exec.run_command(runtime_sync.with_runtime_sync(command))
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
