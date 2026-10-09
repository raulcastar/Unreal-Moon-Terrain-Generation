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

Discovery runs once per process: the session that finds the editor (UDP pings,
one a second) stays open between actions, so an action starts as soon as the
editor is known instead of waiting out a fresh discovery each time. The command
connection is still opened per action and closed after it: it is a listening
port on this machine (6776) that the editor connects back to, and holding it
would lock out every other Remote Execution client (scripts, IDE plugins).
"""

import atexit
import os
import threading
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


_session = None                 # the long-lived discovery session (remote_execution.RemoteExecution)
_session_lock = threading.Lock()   # one action at a time: there is one command port


def _stop_session():
    global _session
    if _session is not None:
        try:
            _session.stop()
        except Exception:
            pass                    # already broken; a new session replaces it
        _session = None


atexit.register(_stop_session)


def _find_editor(discovery_seconds):
    """
    The node id of the first Unreal Editor the session knows, waiting up to
    `discovery_seconds` for one to answer (no wait once it is known), or None.
    """
    global _session
    if _session is None:
        _session = remote_execution.RemoteExecution()
        _session.start()
    deadline = time.monotonic() + discovery_seconds
    while True:
        nodes = _answering_nodes()
        if nodes:
            return nodes[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


ANSWERED_WITHIN_S = 2.5         # the session pings every second


def _answering_nodes():
    """
    Ids of the editors that answered a ping lately, freshest first. The session
    forgets an editor only 5 s after its last answer; connecting to one that has
    just closed would wait 30 s for it, so only recent answers count.
    """
    try:
        known = _session._broadcast_connection._nodes._remote_nodes    # id -> node, with _last_pong
        now = time.time()
        recent = [(node._last_pong, node_id) for node_id, node in list(known.items())
                  if now - node._last_pong <= ANSWERED_WITHIN_S]
        return [node_id for _pong, node_id in sorted(recent, reverse=True)]
    except AttributeError:      # a different remote_execution: its public list instead
        return [n.get("node_id") for n in _session.remote_nodes]


def run_in_unreal(command, log=print, action="Running in Unreal",
                  discovery_seconds=2.0):
    """
    Runs `command` in the first responding Unreal Editor, relaying its output to
    `log`. Blocks until completion; returns True on success. `discovery_seconds`
    is how long to wait for an editor to answer when none is known yet.
    """
    with _session_lock:
        node_id = _find_editor(discovery_seconds)
        if node_id is None:
            log("Searching for a running Unreal Editor...")
            node_id = _find_editor(discovery_seconds)      # one more period
        if node_id is None:
            log("[-] No Unreal instance detected. Enable the Python Editor Script "
                "Plugin and 'Remote Execution' in Project Settings.", "error")
            return False
        log(f"{action} (node {node_id})...")
        try:
            _session.open_command_connection(node_id)
            # Also refreshes the project's own copy of the editor watcher (runtime_sync).
            result = _session.run_command(runtime_sync.with_runtime_sync(command))
        except Exception as exc:
            # The editor closed or restarted mid-action: start discovery afresh next time.
            _stop_session()
            log(f"[-] Lost the connection to Unreal: {exc}", "error")
            return False
        finally:
            if _session is not None:
                _session.close_command_connection()

    for entry in result.get("output", []):
        text = str(entry.get("output", "")).strip()
        if text:
            level = {"Warning": "warn", "Error": "error"}.get(entry.get("type"))
            log(text, level)
    if not result.get("success", False):
        log(f"[-] Failed in Unreal: {result.get('result', '')}", "error")
        return False
    return True
