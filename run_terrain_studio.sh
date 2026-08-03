#!/usr/bin/env bash
# Lunar Terrain Studio launcher (Linux / macOS).
# Runs the studio with python3; set PYTHON=... to use a specific interpreter.
# Make it executable once with:  chmod +x run_terrain_studio.sh

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"

"$PY" "$DIR/Terrain_Studio/terrain_studio.py" "$@"
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "[-] The studio exited with an error."
    echo "    If a module is missing, install the dependencies with:"
    echo "        $PY -m pip install numpy scipy pandas pyyaml matplotlib tifffile"
    echo "    On Debian/Ubuntu, Tkinter is a separate package:"
    echo "        sudo apt install python3-tk"
fi

exit $status
