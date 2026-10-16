"""
Lunar Terrain Studio
--------------------
Graphical front-end for the procedural lunar terrain pipeline. A home screen
leads to each step of the workflow:

  1. Create & Send Terrain   procedural DEM with a fast preview, then the 16-bit
                             heightmap and the Landscape in a running Unreal Editor
  2. Rocks & Rover Paths     a rock field around drivable rover corridors, placed
                             in Unreal and tagged for the object mask
  3. Crater & Shadow Masks   the crater map and shadow flags of the rover's
                             object-mask camera
  4. Sun & Shadows           a lunar Directional Light set from the shadow wanted

plus a map browser and the conversion of an existing DEM. Unreal is reached
over its Remote Execution protocol. Needs numpy, scipy, pandas, pyyaml,
matplotlib and tkinter.

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
