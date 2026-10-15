"""
MCP server for Lunar Terrain Studio: lets an AI agent generate lunar DEMs, plan
rock fields, and build them in a running Unreal Editor (Landscape, rocks, sun),
and start / stop Play. It calls the studio's own modules, the same code the GUI
runs, and writes to the same outputs/ folder.

Run (stdio):  py mcp/terrain_studio_mcp.py
Claude Code:  claude mcp add terrain-studio -- py "<repo>/mcp/terrain_studio_mcp.py"

Unreal tools need the editor running with the Python Editor Script Plugin and
Remote Execution enabled (Project Settings). Tools that change the level refuse
while Play is running.
"""

import contextlib
import glob
import io
import json
import os
import sys
import time

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO = os.path.join(REPOSITORY, 'Terrain_Studio')
for _path in (STUDIO, os.path.join(STUDIO, 'generator'), os.path.join(STUDIO, 'unreal_link')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

import rock_field  # noqa: E402
import send_masks  # noqa: E402
import send_rocks  # noqa: E402
import send_sun  # noqa: E402
import send_terrain  # noqa: E402
import terrain_core  # noqa: E402
import unreal_connection  # noqa: E402
from gui import paths  # noqa: E402
from gui.dem_files import dem_group_name, load_dem_array, scan_dem_folder  # noqa: E402

MAX_CELLS = 4097 * 4097          # one Unreal Landscape; larger maps fall back to a manual import
STATE_PREFIX = 'MCP_STATE '

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
LEVEL = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

server = MCPServer(
    name='terrain-studio',
    instructions=(
        "Lunar Terrain Studio: procedural lunar DEMs (elevation maps), rock fields with "
        "rover corridors, and building them in Unreal. Typical flow: list_presets -> "
        "generate_terrain -> plan_rocks -> unreal_status -> send_terrain_to_unreal -> "
        "send_rocks_to_unreal -> set_sun -> unreal_play. Files go to outputs/<name>/. "
        "DEM paths may be absolute or relative to the repository, outputs/ or input_maps/. "
        "Distances are metres, angles degrees. Level-changing tools refuse while Unreal "
        "is in Play: call unreal_stop first."),
)


# =============================================================================
# Helpers
# =============================================================================

@contextlib.contextmanager
def _captured(lines):
    """The studio prints its progress: keep it off stdout (the MCP channel) and return it."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield
    lines.extend(line for line in buffer.getvalue().splitlines() if line.strip())


def _in_unreal(command, action):
    """Runs Python in the editor; returns (ok, output lines). The studio's own connection."""
    lines = []

    def log(text, level=None):
        lines.append(f'[{level}] {text}' if level else text)

    with _captured(lines):
        ok = unreal_connection.run_in_unreal(command, log, action)
    return ok, lines


def _unreal_state():
    """{level, playing, landscapes, rovers} of the open level. ToolError if Unreal does not answer."""
    command = (
        "import json, unreal\n"
        "les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)\n"
        "ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)\n"
        "playing = bool(les.is_in_play_in_editor())\n"
        "world = ues.get_game_world() if playing else ues.get_editor_world()\n"
        "if playing and world:   # the Play world's actors (the editor's are not listed during Play)\n"
        "    actors = unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor)\n"
        "else:\n"
        "    actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()\n"
        "name = world.get_name() if world else None\n"
        "if name and name.startswith('UEDPIE_'):\n"
        "    name = name.split('_', 2)[2]\n"
        "print('" + STATE_PREFIX + "' + json.dumps({\n"
        "    'level': name,\n"
        "    'playing': playing,\n"
        "    'landscapes': [a.get_actor_label() for a in actors if isinstance(a, unreal.Landscape)],\n"
        "    'rovers': sorted({a.get_actor_label() for a in actors if isinstance(a, unreal.Pawn)}),\n"
        "}))\n")
    ok, lines = _in_unreal(command, 'Reading the editor state')
    for line in lines:
        if STATE_PREFIX in line:
            return json.loads(line.split(STATE_PREFIX, 1)[1])
    raise ToolError('Unreal did not answer. Is the editor running with the Python Editor Script '
                    'Plugin and Remote Execution enabled (Project Settings)?\n' + '\n'.join(lines[-5:]))


def _require_editing():
    state = _unreal_state()
    if state['playing']:
        raise ToolError('Unreal is in Play: call unreal_stop first (level changes during Play are lost '
                        'or unsafe).')
    return state


def _resolve_dem(path):
    """An absolute path, 'outputs/...' or 'input_maps/...' (the studio's folders), or a path in either."""
    relative = path.replace('\\', '/')
    candidates = [path]
    for prefix, root in (('outputs/', paths.OUTPUT_ROOT), ('input_maps/', paths.INPUT_MAPS_DIR)):
        if relative.startswith(prefix):
            candidates.append(os.path.join(root, relative[len(prefix):]))
    candidates += [os.path.join(paths.OUTPUT_ROOT, path), os.path.join(paths.INPUT_MAPS_DIR, path),
                   os.path.join(REPOSITORY, path)]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise ToolError(f'DEM not found: {path}. list_dems shows the available ones.')


def _load_dem(path):
    try:
        return load_dem_array(path)
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def _override(params, overrides, known):
    unknown = sorted(set(overrides or {}) - set(known))
    if unknown:
        raise ToolError(f'Unknown parameters {unknown}; valid: {sorted(known)}')
    params.update(overrides or {})
    return params


def _presets():
    return {os.path.splitext(os.path.basename(p))[0]: p
            for p in sorted(glob.glob(os.path.join(paths.CONFIG_DIR, '*.yaml')))}


# =============================================================================
# Terrain and rock planning (files only)
# =============================================================================

@server.tool(annotations=READ)
def list_presets() -> dict:
    """Terrain presets (config/*.yaml) with their parameters, and the parameter defaults."""
    out = {}
    for name, path in _presets().items():
        try:
            out[name] = terrain_core.load_config(path)
        except Exception as exc:
            out[name] = {'error': str(exc)}
    return {'presets': out, 'defaults': terrain_core.default_params(),
            'rock_defaults': rock_field.default_params()}


@server.tool(annotations=WRITE)
def generate_terrain(name: str, preset: str = 'settings', seed: int | None = None,
                     overrides: dict | None = None, overwrite: bool = False) -> dict:
    """
    Generates a lunar DEM from a preset (list_presets) and writes it to
    outputs/<name>/: <name>_elevationMap.npy/.csv, <name>_soilMap.csv (the GNC
    planner's maps) and <name>_craters.csv. overrides: preset keys to change, e.g.
    {"ROWS": 500, "COLS": 500, "CRATER_DENSITY": 0.0006}. A seed reproduces a
    surface. Refuses an existing name unless overwrite. Large maps take minutes.
    """
    presets = _presets()
    if preset not in presets:
        raise ToolError(f'Unknown preset {preset!r}; one of {sorted(presets)}')
    if not name or os.sep in name or '/' in name:
        raise ToolError('name must be a plain folder name, e.g. "crater_field_1".')
    group = dem_group_name(name)
    target = os.path.join(paths.GENERATOR_OUTPUT_DIR, group)
    if os.path.isdir(target) and os.listdir(target) and not overwrite:
        raise ToolError(f'outputs/{group} already exists: pick another name or pass overwrite=true.')
    params = _override(terrain_core.load_config(presets[preset]), overrides, terrain_core.DEFAULTS)
    params = terrain_core.coerce_params(params)
    if params['ROWS'] * params['COLS'] > MAX_CELLS:
        raise ToolError(f"{params['ROWS']}x{params['COLS']} is larger than one Unreal Landscape "
                        '(4097x4097): use fewer ROWS/COLS.')
    lines = []
    t0 = time.monotonic()
    with _captured(lines):
        result = terrain_core.generate_terrain(params, seed=seed)
        written = terrain_core.export_outputs(
            result['elevation'], paths.GENERATOR_OUTPUT_DIR, base_name=f'{group}_elevationMap',
            export_csv=params['EXPORT_CSV'], export_npy=params['EXPORT_NPY'],
            craters=result['craters'], params=params)
    elevation = result['elevation']
    return {'name': group, 'seconds': round(time.monotonic() - t0, 1), 'seed': result['seed'],
            'size_px': [int(result['rows']), int(result['cols'])],
            'resolution_m_per_px': params['RESOLUTION_M_PER_PX'], 'craters': result['crater_count'],
            'elevation_m': {'min': round(float(np.min(elevation)), 2), 'max': round(float(np.max(elevation)), 2)},
            'files': written}


@server.tool(annotations=READ)
def list_dems() -> dict:
    """DEM files in input_maps/ and outputs/ (paths usable by the other tools)."""
    found = {}
    for root, label in ((paths.INPUT_MAPS_DIR, 'input_maps'), (paths.OUTPUT_ROOT, 'outputs')):
        if os.path.isdir(root):
            found[label] = [f'{label}/{name}' for name, _p in scan_dem_folder(root)]
    return found


@server.tool(annotations=READ)
def describe_dem(path: str, resolution_m_per_px: float = 1.0) -> dict:
    """Size, extent, elevation range and slope statistics of a DEM."""
    full = _resolve_dem(path)
    dem = _load_dem(full)
    gy, gx = np.gradient(dem, resolution_m_per_px)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    return {'path': full, 'size_px': list(dem.shape),
            'extent_m': [round(dem.shape[0] * resolution_m_per_px, 1), round(dem.shape[1] * resolution_m_per_px, 1)],
            'elevation_m': {'min': round(float(np.nanmin(dem)), 2), 'max': round(float(np.nanmax(dem)), 2),
                            'mean': round(float(np.nanmean(dem)), 2)},
            'slope_deg': {'mean': round(float(np.nanmean(slope)), 2), 'p95': round(float(np.nanpercentile(slope, 95)), 2),
                          'over_15_pct': round(float(np.nanmean(slope > 15.0) * 100), 1)},
            'note': 'The GNC planner treats slopes over 15 deg as impassable.'}


@server.tool(annotations=WRITE)
def plan_rocks(dem_path: str, resolution_m_per_px: float = 1.0, seed: int | None = None,
               overrides: dict | None = None, obstacle_map: bool = False) -> dict:
    """
    Plans a rock field with connected rover corridors on a DEM and writes
    outputs/<group>/<group>_rocks.csv and _roverPaths.csv (and _obstacleMap.csv).
    overrides: rock parameters (list_presets rock_defaults), e.g. {"ROCK_COVERAGE": 0.05}.
    """
    full = _resolve_dem(dem_path)
    dem = _load_dem(full)
    params = rock_field.coerce_params(_override(rock_field.default_params(), overrides, rock_field.DEFAULTS))
    group = dem_group_name(os.path.splitext(os.path.basename(full))[0])
    lines = []
    with _captured(lines):
        result = rock_field.build_rock_field(dem, resolution_m_per_px, params, seed=seed)
        written = rock_field.export_rock_field(result, paths.GENERATOR_OUTPUT_DIR, f'{group}_elevationMap',
                                               export_obstacle_map=obstacle_map)
    return {'group': group, 'rocks': result['rock_count'], 'capped': bool(result.get('rock_capped')),
            'drivable_pct': round(result['drivable_frac'] * 100, 1),
            'crater_pct': round(result['crater_frac'] * 100, 1),
            'free_after_rocks_pct': round(result['free_frac'] * 100, 1),
            'corridors_connected': bool(result['connected']), 'corridor_segments': result['road_segments'],
            'files': written}


# =============================================================================
# Unreal
# =============================================================================

@server.tool(annotations=READ)
def unreal_status() -> dict:
    """The open Unreal level, whether Play is running, its Landscapes and its rovers (pawns)."""
    return _unreal_state()


def _play(begin):
    state = _unreal_state()
    if state['playing'] == begin:
        return {**state, 'note': 'already ' + ('playing' if begin else 'stopped')}
    call = 'editor_request_begin_play' if begin else 'editor_request_end_play'
    _in_unreal(f"import unreal\nunreal.get_editor_subsystem(unreal.LevelEditorSubsystem).{call}()\n",
               'Starting Play' if begin else 'Stopping Play')
    t0 = time.monotonic()
    while time.monotonic() - t0 < 20.0:
        time.sleep(1.0)
        state = _unreal_state()
        if state['playing'] == begin:
            break
    return state


@server.tool(annotations=WRITE)
def unreal_play() -> dict:
    """
    Starts Play in the editor (the simulation: the rover spawns, sensors stream to the
    GNC stack). The rover settles for ~10 s after Play starts before it should drive.
    """
    state = _play(True)
    if state['playing']:
        state['note'] = 'Playing. Wait ~10 s for the rover to land before driving.'
    return state


@server.tool(annotations=WRITE)
def unreal_stop() -> dict:
    """Stops Play in the editor."""
    return _play(False)


@server.tool(annotations=LEVEL)
def send_terrain_to_unreal(dem_path: str, resolution_m_per_px: float = 1.0, save_level: bool = True) -> dict:
    """
    Converts a DEM to a 16-bit heightmap (outputs/<group>/) and builds a Landscape
    from it in the open Unreal level (with the simulator's material and mask
    tag), then saves the level. Adds a Landscape: an existing one is not removed.
    Refuses during Play.
    """
    full = _resolve_dem(dem_path)
    before = _require_editing()
    group_dir = send_terrain.output_group_directory(full)
    os.makedirs(group_dir, exist_ok=True)
    lines = []
    with _captured(lines):
        config, interpolation, z_scale, heightmap, z_offset = send_terrain.generate_lunar_terrain(
            full, group_dir, resolution_m_per_px)
        send_terrain.send_to_unreal(config, interpolation, z_scale, heightmap, z_offset=z_offset,
                                    output_directory=group_dir, save_level=save_level)
    failed = any('[-]' in line for line in lines)
    if failed:
        raise ToolError('Unreal did not build the Landscape:\n' + '\n'.join(lines[-12:]))
    return {'heightmap': os.path.normpath(os.path.join(group_dir, heightmap)), 'level': before['level'],
            'landscape_label': f'{os.path.splitext(os.path.basename(full))[0]}_heightmap',
            'saved': save_level, 'log': lines[-12:]}


@server.tool(annotations=LEVEL)
def send_rocks_to_unreal(dem_path: str, models_folder: str, resolution_m_per_px: float = 1.0,
                         seed: int = 1, simulate_physics: bool = False, save_level: bool = True) -> dict:
    """
    Places the rock field planned by plan_rocks for this DEM on its Landscape in
    Unreal, using the rock meshes in models_folder (a folder of .fbx/.obj/.glb
    models on disk). The DEM's Landscape must exist (send_terrain_to_unreal).
    Thousands of rocks take minutes. Refuses during Play.
    """
    full = _resolve_dem(dem_path)
    if not os.path.isdir(models_folder):
        raise ToolError(f'models_folder not found: {models_folder}')
    stem = os.path.splitext(os.path.basename(full))[0]
    group = dem_group_name(stem)
    rocks_csv = os.path.join(paths.GENERATOR_OUTPUT_DIR, group, f'{group}_rocks.csv')
    if not os.path.isfile(rocks_csv):
        raise ToolError(f'No {group}_rocks.csv yet: run plan_rocks on this DEM first.')
    state = _require_editing()
    label = f'{stem}_heightmap'
    if label not in state['landscapes']:
        raise ToolError(f'No Landscape {label} in level {state["level"]} (it has {state["landscapes"]}): '
                        'run send_terrain_to_unreal first.')
    import heightmap_files
    width = heightmap_files.heightmap_width(os.path.join(paths.GENERATOR_OUTPUT_DIR, group), stem)
    lines = []

    def log(text, level=None):
        lines.append(f'[{level}] {text}' if level else text)

    with _captured(lines):
        ok = send_rocks.send_rocks_to_unreal(rocks_csv, models_folder, resolution_m_per_px,
                                             landscape_label=label, landscape_width=width, seed=seed,
                                             simulate_physics=simulate_physics, save_level=save_level, log=log)
    if not ok:
        raise ToolError('Unreal did not place the rocks:\n' + '\n'.join(lines[-12:]))
    return {'placed_from': rocks_csv, 'landscape': label, 'saved': save_level, 'log': lines[-12:]}


@server.tool(annotations=READ)
def get_sun() -> dict:
    """The level's sun: azimuth and elevation (deg), intensity (lux), source angle, and whether Play runs."""
    lines = []
    with _captured(lines):
        sun = send_sun.read_sun_from_unreal(log=lambda text, level=None: lines.append(text))
    if sun is None:
        raise ToolError('Unreal did not answer, or the level has no directional light:\n' + '\n'.join(lines[-5:]))
    return sun


@server.tool(annotations=LEVEL)
def set_sun(azimuth_deg: float, elevation_deg: float, intensity_lux: float = 120000.0,
            save_level: bool = True) -> dict:
    """
    Sets the lunar sun: azimuth (deg, compass), elevation above the horizon (deg;
    low = long shadows), intensity (lux; 120000 is lunar noon). Saves the level.
    """
    if not -10.0 <= elevation_deg <= 90.0:
        raise ToolError('elevation_deg must be between -10 and 90.')
    lines = []
    with _captured(lines):
        ok = send_sun.send_sun_to_unreal(azimuth_deg, elevation_deg, intensity_lux, save_level=save_level,
                                         log=lambda text, level=None: lines.append(text))
    if not ok:
        raise ToolError('Unreal did not set the sun:\n' + '\n'.join(lines[-8:]))
    return {'azimuth_deg': azimuth_deg, 'elevation_deg': elevation_deg, 'intensity_lux': intensity_lux,
            'saved': save_level}


@server.tool(annotations=READ)
def list_mask_cameras() -> dict:
    """The level's scene-capture cameras, and which one streams the object mask (ground truth)."""
    lines = []
    with _captured(lines):
        found = send_masks.list_mask_cameras(log=lambda text, level=None: lines.append(text))
    if found is None:
        raise ToolError('Unreal did not answer:\n' + '\n'.join(lines[-5:]))
    return found


if __name__ == '__main__':
    server.run()
