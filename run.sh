#!/usr/bin/env bash
# One command: make sure the conda env exists, log in to Plaud if needed (once, in the
# browser), download every recording, transcribe, summarise, and open the local viewer.
#
#   ./run.sh                 # everything
#   ./run.sh --limit 5       # try the 5 newest first
#   ./run.sh --no-local      # skip local Whisper, only fetch audio + Plaud transcripts
#   ./run.sh --help          # all options
set -euo pipefail
cd "$(dirname "$0")"

ENV_NAME="plaud_transcripts"
# Plaud's official CLI is used only for the browser login; pinned to a reviewed version.
PLAUD_CLI_VERSION="${PLAUD_CLI_VERSION:-0.3.15}"

die() { echo "ERROR: $*" >&2; exit 1; }

# --- conda -------------------------------------------------------------------
CONDA="${CONDA_EXE:-$(command -v conda 2>/dev/null || true)}"
if [ -z "$CONDA" ] || [ ! -x "$CONDA" ]; then
  for base in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/miniforge3" \
              /opt/homebrew/anaconda3 /opt/homebrew/Caskroom/miniconda/base /opt/conda; do
    if [ -x "$base/bin/conda" ]; then CONDA="$base/bin/conda"; break; fi
  done
fi
[ -n "$CONDA" ] && [ -x "$CONDA" ] || die "conda not found. Install conda or set CONDA_EXE."

if ! "$CONDA" env list | awk '{print $1}' | grep -qx "$ENV_NAME"; then
  [ -f environment.yml ] || die "environment.yml is missing."
  echo "Creating conda environment '$ENV_NAME' (first run only) ..."
  "$CONDA" env create -f environment.yml
fi
ENV_PREFIX="$("$CONDA" env list | awk -v name="$ENV_NAME" '$1 == name { print $NF }')"
if [ environment.yml -nt "$ENV_PREFIX/.plaud_env_stamp" ]; then   # environment.yml changed
  echo "Updating conda environment '$ENV_NAME' ..."
  "$CONDA" env update -n "$ENV_NAME" -f environment.yml
  touch "$ENV_PREFIX/.plaud_env_stamp"
fi
export PATH="$ENV_PREFIX/bin:$PATH"   # the env's python and ffmpeg
PY="$ENV_PREFIX/bin/python"

# --- login -------------------------------------------------------------------
login() {
  if [ -n "${NO_LOGIN:-}" ]; then   # unattended run: nobody is there to approve in the browser
    command -v osascript >/dev/null 2>&1 && osascript -e \
      'display notification "Plaud login expired. Run ./run.sh once to log in again." with title "Plaud transcripts"' || true
    die "Plaud login is missing or expired. Run ./run.sh by hand once to log in."
  fi
  command -v npx >/dev/null 2>&1 || die "npx (Node.js) is needed once for the Plaud login."
  echo
  echo "Plaud login: your browser will open. Approve access there, then come back."
  PLAUD_TELEMETRY_DISABLED=1 npx --yes "@plaud-ai/cli@${PLAUD_CLI_VERSION}" login \
    || die "Plaud login failed."
}

case " $* " in *" --help "*|*" -h "*) exec "$PY" sync.py --help ;; esac

# --- one run at a time (a scheduled run and a manual one must not overlap) ------
LIBRARY="${PLAUD_LIBRARY:-$PWD/library}"
LOCK="$LIBRARY/.run.lock"
mkdir -p "$LIBRARY"
if ! mkdir "$LOCK" 2>/dev/null; then
  other="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
    echo "Another run is in progress (pid $other). Nothing to do."
    exit 0
  fi
  rm -rf "$LOCK"; mkdir "$LOCK"   # stale lock from a run that was killed
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

[ -f "${PLAUD_TOKEN_FILE:-$HOME/.plaud/tokens.json}" ] || login

# --- sync --------------------------------------------------------------------
set +e
"$PY" sync.py "$@"; rc=$?
if [ "$rc" -eq 2 ]; then   # saved login expired or was revoked: log in again and retry once
  set -e; login; set +e
  "$PY" sync.py "$@"; rc=$?
fi
set -e

if [ -f "$LIBRARY/index.html" ] && [ -z "${NO_OPEN:-}" ] && command -v open >/dev/null 2>&1; then
  open "$LIBRARY/index.html"
fi
exit "$rc"
