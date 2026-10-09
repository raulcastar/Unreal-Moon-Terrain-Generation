"""
Lunar Terrain Studio
--------------------
Graphical front-end for the procedural lunar DEM pipeline.

Three stages are exposed in a single window:

  1. PARAMETERS  - every knob of the procedural simulation, with live tooltips
                   and YAML preset load/save (shared with the CLI generator).
  2. PREVIEW     - a fast, reduced-resolution render of the exact same pipeline,
                   shown as a sun-lit shaded relief, a 3D surface, and a slope
                   analysis, so the surface can be judged before committing to a
                   full-resolution run.
  3. UNREAL      - full-resolution generation, conversion to a 16-bit .r16
                   heightmap, and dispatch to a running Unreal Editor over the
                   Remote Execution protocol.

Dependencies are limited to what the pipeline already requires (numpy, scipy,
pandas, pyyaml, matplotlib) plus tkinter from the standard library.

Run with:  python Terrain_Studio/terrain_studio.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # For the gui package

from gui.app import main  # noqa: E402

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
