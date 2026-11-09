# Lunar Terrain Studio in a container: the Tk app, its MCP server, the CLI
# generator and the tests, with nothing installed on the host but Docker.
# run_terrain_studio_docker.sh builds and runs it (GUI, mcp, test, cli).
#
# Python 3.14, the version the pins are tested with. The python images build
# _tkinter but ship without the Tk runtime, hence libtk8.6; the fonts replace
# the Windows UI fonts (Segoe UI, Consolas) the studio asks for.
FROM python:3.14-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libtk8.6 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/req/requirements.txt
COPY mcp/requirements.txt /tmp/req/mcp/requirements.txt
COPY requirements-dev.txt /tmp/req/requirements-dev.txt
RUN pip install --no-cache-dir --disable-pip-version-check \
      -r /tmp/req/requirements-dev.txt -r /tmp/req/mcp/requirements.txt \
 && rm -rf /tmp/req
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
