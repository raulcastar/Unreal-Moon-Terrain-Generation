"""
Every studio module imports cleanly.

Most pages (landscape_page, datasets_page...) are not imported by any other
test, so a syntax error or a broken import in one would only show when the app
starts. Importing creates no window and touches no Unreal or network.
"""

import importlib
import os

import pytest

from conftest import STUDIO

GUI_MODULES = sorted(
    f"gui.{os.path.splitext(name)[0]}"
    for name in os.listdir(os.path.join(STUDIO, "gui"))
    if name.endswith(".py") and name != "__init__.py")


@pytest.mark.parametrize("module", GUI_MODULES)
def test_gui_module_imports(module):
    importlib.import_module(module)


def test_unreal_side_commands_compile():
    """The Unreal-side commands need the editor to run, but must at least compile."""
    folder = os.path.join(STUDIO, "unreal_commands")
    for name in os.listdir(folder):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                compile(handle.read(), name, "exec")
