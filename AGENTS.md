# AGENTS.md: Unreal Moon Terrain Generation (Lunar Terrain Studio)

Guidance for AI coding agents working in this repository. The README has the
full documentation; this file covers what an agent needs to work safely.

## What this is

Lunar Terrain Studio is a Tk desktop app (`Terrain_Studio/terrain_studio.py`).
It generates procedural lunar DEMs (elevation maps), plans rock fields with
rover corridors, makes ground-truth masks and sets the sun. It then builds all
of these in a running Unreal Editor over Unreal's Remote Execution. The
simulator and rover stack that use them are in the sibling repo
`GNC-Moon-Simulator`.

## Layout

The code is split by where it runs; the README's "Project layout" has the full list.

| Path | Runs in | What |
|---|---|---|
| `Terrain_Studio/gui/` | studio | Tk views (`*_page.py`), their Tk-free logic (`*_logic.py`), plots |
| `Terrain_Studio/generator/` | studio | Terrain, craters, rocks, sun maths; no Unreal |
| `Terrain_Studio/unreal_link/` | studio | Senders: they build Python and run it in the editor (`unreal_connection.run_in_unreal`) |
| `Terrain_Studio/unreal_commands/` | Unreal | What the senders run inside the editor |
| `Terrain_Studio/unreal_runtime/` | Unreal | The per-frame editor watcher, copied to `<Project>/Content/Python` |
| `mcp/terrain_studio_mcp.py` | studio | MCP server for agents, which calls the modules above |
| `config/` | | Terrain presets (`*.yaml`), `mask_classes.json` |
| `input_maps/`, `outputs/<name>/` | | Source DEMs; everything generated (untracked) |

## Run and test

```sh
py -m pip install -r requirements-dev.txt   # once (Windows; python3 elsewhere)
run_terrain_studio.bat                      # the app
py -m pytest                                # all tests, from the repo root (~10 s)
py -m pytest tests/test_terrain_mcp.py      # one file; -k <name> also works
```

- `py` is Python 3.14 here; its packages come from `requirements.txt`. If the
  app fails at startup with `ModuleNotFoundError`, install the requirements.
- `generator/generate_cli.py` runs code when it is imported: it loads the
  config and creates the output folder. Only a command-line run reads
  `sys.argv`. Keep it that way, because the test setup imports it.

## Rules that prevent real damage

These come from actual incidents.

- **Tests must never write to the real `outputs/`.** A test once deleted it.
  `tests/conftest.py` redirects every output path to a temporary folder, and
  fails the run if `outputs/` changed. Keep new tests inside that setup, and
  read output paths at call time (`paths.GENERATOR_OUTPUT_DIR`), not at import.
- **Tests must never reach a real Unreal Editor.** A test once reached the
  reopened editor and saved changes into the user's level. Stub
  `run_in_unreal` in every sender module that imports it, as
  `tests/test_terrain_mcp.py` does.
- **Never delete, duplicate or rename an Unreal level by script.** It crashed
  the editor. Ask the user to use Save Level As.
- **Don't change the level while Unreal is in Play.** Changes made during Play
  are lost or unsafe. Never set properties on the rover's components during
  Play: it broke the wheels' physics.
- **Unreal-side code** (`unreal_commands/`, `unreal_runtime/`) runs in the
  editor's own Python. It must not import the studio's dependencies (numpy is
  fine there, but tk, scipy and skimage are not).
- **Don't commit or push unless the user asks.**

## Conventions

- Keep the logic Tk-free in `*_logic.py` and `generator/`, so it can be
  tested; the pages only lay out and wire events.
- Comments explain *why*, in full sentences.
- When you change behaviour, update the README section that documents it.

## Using the studio as an agent (MCP)

`mcp/terrain_studio_mcp.py` exposes the studio as tools:

- **Files:** `list_presets`, `generate_terrain`, `list_dems`, `describe_dem`,
  `plan_rocks`.
- **Unreal:** `unreal_status`, `unreal_play`, `unreal_stop`,
  `send_terrain_to_unreal`, `send_rocks_to_unreal`, `get_sun`, `set_sun`,
  `list_mask_cameras`.

`.mcp.json` registers it for Claude Code. Setup:
`py -m pip install -r mcp/requirements.txt`. Unreal tools need the editor
running with the Python Editor Script Plugin and Remote Execution enabled.
Tools that change the level refuse during Play, and `generate_terrain` won't
overwrite an existing `outputs/<name>` unless asked. Driving the rover is done
by the GNC server (`GNC-Moon-Simulator/mcp`).
