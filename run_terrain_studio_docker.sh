#!/usr/bin/env bash
# Lunar Terrain Studio in Docker (Linux). Builds the image on first use.
#
#   ./run_terrain_studio_docker.sh            the studio (Tk GUI on your X server)
#   ./run_terrain_studio_docker.sh test       the test suite, headless
#   ./run_terrain_studio_docker.sh mcp        the MCP server on stdio (.mcp.json)
#   ./run_terrain_studio_docker.sh cli ARGS   generator/generate_cli.py ARGS
#
# The repository is mounted at its own path: the commands the studio sends to
# Unreal name its modules and outputs by absolute path, so the editor (native,
# or in MARE-Lunar-Simulator's container, which mounts this repository at the
# same path) must see them there. Host networking because Python Remote
# Execution (UDP multicast 239.0.0.1:6766, then TCP 6776) is bound to
# 127.0.0.1. Runs as your user, so outputs/ stays yours.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${TERRAIN_STUDIO_IMAGE:-lunar_terrain_studio:dev}"

# Quiet, and on stderr: in mcp mode stdout is the protocol stream.
if [[ -n "${REBUILD:-}" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -q -t "$IMAGE" "$DIR" >&2
fi

args=(--rm --network host --user "$(id -u):$(id -g)" -v "$DIR:$DIR" -w "$DIR")
mode="${1:-gui}"
[[ $# -gt 0 ]] && shift
case "$mode" in
    gui)
        x11=(-e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro)
        if [[ -n "${XAUTHORITY:-}" && -f "$XAUTHORITY" ]]; then
            x11+=(-e XAUTHORITY=/tmp/.Xauthority -v "$XAUTHORITY:/tmp/.Xauthority:ro")
        fi
        exec docker run -it "${args[@]}" "${x11[@]}" "$IMAGE" python Terrain_Studio/terrain_studio.py "$@" ;;
    test)
        exec docker run "${args[@]}" -e MPLBACKEND=Agg "$IMAGE" python -m pytest -p no:cacheprovider "$@" ;;
    mcp)
        exec docker run -i "${args[@]}" -e MPLBACKEND=Agg "$IMAGE" python mcp/terrain_studio_mcp.py "$@" ;;
    cli)
        exec docker run "${args[@]}" -e MPLBACKEND=Agg -w "$DIR/Terrain_Studio/generator" "$IMAGE" python generate_cli.py "$@" ;;
    *)
        echo "usage: $0 [gui|test|mcp|cli ARGS]" >&2; exit 2 ;;
esac
