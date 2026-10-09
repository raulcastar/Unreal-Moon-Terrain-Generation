"""
Shared test setup: import paths, and two guards for the repository's real data.

1. Every test runs with the studio's output folders (gui.paths, send_terrain,
   generate_cli) redirected to a fresh temporary folder (`outputs_dir`).
2. The real outputs/ folder is fingerprinted before the session and compared
   after it: any file added, removed or changed there fails the run. A test
   once deleted the real outputs/, so this is checked, not assumed.
"""

import os
import sys

import pytest

os.environ.setdefault("MPLBACKEND", "Agg")      # generate_cli imports pyplot

REPOSITORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUDIO = os.path.join(REPOSITORY, "Terrain_Studio")
for _path in (STUDIO, os.path.join(STUDIO, "generator"), os.path.join(STUDIO, "unreal_link")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

REAL_OUTPUTS = os.path.join(REPOSITORY, "outputs")


def _fingerprint(folder):
    """{relative path: (size, mtime)} of every file under `folder`."""
    found = {}
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            stat = os.stat(path)
            found[os.path.relpath(path, folder)] = (stat.st_size, stat.st_mtime_ns)
    return found


@pytest.fixture(scope="session", autouse=True)
def real_outputs_untouched():
    """Fails the session if the real outputs/ folder changed during it."""
    before = _fingerprint(REAL_OUTPUTS)
    yield
    after = _fingerprint(REAL_OUTPUTS)
    changed = sorted(set(before) ^ set(after)
                     | {p for p in set(before) & set(after) if before[p] != after[p]})
    assert not changed, f"the tests changed the real outputs/ folder: {changed[:10]}"


@pytest.fixture(autouse=True)
def outputs_dir(tmp_path, monkeypatch):
    """A temporary outputs folder that every output path of the studio points at."""
    folder = tmp_path / "outputs"
    folder.mkdir()
    from gui import paths
    monkeypatch.setattr(paths, "OUTPUT_ROOT", str(folder))
    monkeypatch.setattr(paths, "GENERATOR_OUTPUT_DIR", str(folder))
    import send_terrain
    monkeypatch.setattr(send_terrain, "BASE_OUTPUT_DIRECTORY", str(folder).replace("\\", "/"))
    import generate_cli
    monkeypatch.setattr(generate_cli, "DEFAULT_OUTPUT_DIR", str(folder))
    return folder
