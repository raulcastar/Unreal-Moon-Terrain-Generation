# Unreal Moon Terrain Generation

Terrain Studio generates procedural lunar elevation maps (DEMs), plans rock
fields with guaranteed rover corridors, and builds them in Unreal Engine 5: the
Landscape, the rocks, ground-truth masks for the rover's cameras, and the sun.
It is part of [GNC-Moon-Simulator](https://github.com/raulcastar/GNC-Moon-Simulator).

<p align="center"><img src="docs/images/home.png" width="720" alt="Terrain Studio's home screen"></p>

## Quick start

```sh
py -m pip install -r requirements.txt     # once (python3 -m pip on Linux / macOS)
run_terrain_studio.bat                    # Windows
./run_terrain_studio.sh                   # Linux / macOS (chmod +x once)
```

Or `python Terrain_Studio/terrain_studio.py` on any platform. Tkinter ships with
Python (`sudo apt install python3-tk` on Debian/Ubuntu).

**Unreal setup:** the steps that send to Unreal need a running Unreal Editor
(5.7) with the *Python Editor Script Plugin* and *Remote Execution* enabled
(Project Settings), and the
[TAPython](https://github.com/cgerchenhp/UE_TAPython_Plugin_Release) plugin,
which builds the Landscape. The
[Unreal-Moon-Simulator](https://github.com/raulcastar/Unreal-Moon-Simulator)
project has them all.

## Workflow

The home screen lays out the steps in order; each opens in the same window, with
a **⌂ Home** button to return. In every view the left column has one tab per
step, with that step's buttons pinned under its settings.

| Step | What it does | |
|---|---|---|
| **1. Create & Send Terrain** | Generates a DEM with a live preview, then builds the Landscape in Unreal | [docs/terrain.md](docs/terrain.md) |
| **2. Rocks & Rover Paths** | Scatters rocks around drivable corridors and places them in Unreal, tagged for the object mask | [docs/rocks.md](docs/rocks.md) |
| **3. Crater & Shadow Masks** | Adds craters, shadows, the sun, wheel tracks and the rover's shadow to the rover's object-mask camera | [docs/masks.md](docs/masks.md) |
| **4. Sun & Shadows** | Sets a lunar sun in Unreal from the shadow you draw | [docs/sun.md](docs/sun.md) |
| **Library** | Browse every map; send any existing DEM to Unreal | [docs/terrain.md](docs/terrain.md#send-to-unreal) |

![Creating a terrain](docs/images/create-terrain.gif)

Everything a map produces goes to `outputs/<name>/`: the DEM and its soil map
(the maps the GNC path planner reads), the crater list, the heightmap, the rock
field and the crater mask.

## Folders

| Folder | Contents | Tracked |
| --- | --- | --- |
| `config/` | Terrain presets (YAML), rock presets (`rocks/`), `mask_classes.json` | yes |
| `input_maps/` | Source DEMs: `.npy`, `.csv`, `.txt`, `.tif` | no |
| `input_rocks/` | Rock models (Poly Haven, glTF) | yes |
| `outputs/<name>/` | Everything generated for one map | no |

## AI agents (MCP)

`mcp/terrain_studio_mcp.py` is an MCP server so an AI agent can use the studio:
generate terrains from the presets, describe DEMs, plan rock fields, and in
Unreal build the Landscape and rocks, set the sun, list the mask cameras, and
start or stop Play. It runs the same code as the GUI.

```sh
py -m pip install -r mcp/requirements.txt                                # once
claude mcp add terrain-studio -- py "<repo>/mcp/terrain_studio_mcp.py"   # or use the repo's .mcp.json
```

Tools that change the level refuse while Unreal is in Play. The GNC repository
has the matching server for driving the rover.

## Documentation

| | |
|---|---|
| [Terrain](docs/terrain.md) | Step 1: parameters, outputs, Send to Unreal, command line |
| [Rocks](docs/rocks.md) | Step 2: how the plan is made, presets, export, placing in Unreal |
| [Masks](docs/masks.md) | Step 3: crater map, object mask ids, shadows, sun, wheel tracks, rover shadow |
| [Sun](docs/sun.md) | Step 4: setting the sun and sending it to Unreal |
| [Editor watcher](docs/editor-watcher.md) | The per-frame helper that runs inside the Unreal project |
| [Development](docs/development.md) | Tests and the code layout |
| [AGENTS.md](AGENTS.md) | Guidance for AI coding agents working in this repo |

## License

[MIT](LICENSE), © 2026 Raul Castilla Arquillo, except:

- `Terrain_Studio/unreal_link/remote_execution.py` is © Epic Games (Unreal
  Engine's Remote Execution client), under the Unreal Engine EULA.
- The rock models in `input_rocks/` are from
  [Poly Haven's Moon collection](https://polyhaven.com/collections/moon),
  [CC0](https://creativecommons.org/publicdomain/zero/1.0/) (public domain).
  The Unreal project's landscape textures come from the same collection.
