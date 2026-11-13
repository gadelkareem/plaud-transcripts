#!/usr/bin/env bash
# Start the plaud-library MCP server (stdio) in the project's conda environment.
# Point an MCP client at this file; see README.md.
set -euo pipefail
cd "$(dirname "$0")"

ENV_NAME="plaud_transcripts"
CONDA="${CONDA_EXE:-$(command -v conda 2>/dev/null || true)}"
if [ -z "$CONDA" ] || [ ! -x "$CONDA" ]; then
  for base in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" \
              /opt/homebrew/anaconda3 /opt/homebrew/Caskroom/miniconda/base /opt/conda; do
    if [ -x "$base/bin/conda" ]; then CONDA="$base/bin/conda"; break; fi
  done
fi
[ -n "$CONDA" ] && [ -x "$CONDA" ] || { echo "conda not found; run ./run.sh once first" >&2; exit 1; }

ENV_PREFIX="$("$CONDA" env list | awk -v name="$ENV_NAME" '$1 == name { print $NF }')"
[ -x "$ENV_PREFIX/bin/python" ] || { echo "conda env '$ENV_NAME' missing; run ./run.sh once first" >&2; exit 1; }
exec "$ENV_PREFIX/bin/python" mcp_server.py
