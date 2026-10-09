"""The guard itself: the studio's output paths point at the test folder, never outputs/."""

import os

from conftest import REAL_OUTPUTS


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def test_every_output_path_is_redirected(outputs_dir):
    from gui import paths
    import generate_cli
    import send_terrain
    for value in (paths.OUTPUT_ROOT, paths.GENERATOR_OUTPUT_DIR,
                  send_terrain.BASE_OUTPUT_DIRECTORY, generate_cli.DEFAULT_OUTPUT_DIR):
        assert _norm(value) == _norm(outputs_dir)
        assert _norm(value) != _norm(REAL_OUTPUTS)


def test_output_group_dir_uses_the_test_folder(outputs_dir):
    from gui import paths
    group = paths.output_group_dir("C:/maps/demo_elevationMap.npy")
    assert _norm(group).startswith(_norm(outputs_dir))


def test_the_outputs_guard_notices_changes(tmp_path):
    """The session guard's fingerprint sees added, changed and removed files."""
    from conftest import _fingerprint
    (tmp_path / "a.csv").write_text("1")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.npy").write_text("2")
    before = _fingerprint(str(tmp_path))
    assert set(before) == {"a.csv", os.path.join("sub", "b.npy")}
    (tmp_path / "a.csv").write_text("changed")
    (tmp_path / "sub" / "b.npy").unlink()
    (tmp_path / "c.png").write_text("3")
    after = _fingerprint(str(tmp_path))
    assert before["a.csv"] != after["a.csv"]
    assert set(before) ^ set(after) == {os.path.join("sub", "b.npy"), "c.png"}
