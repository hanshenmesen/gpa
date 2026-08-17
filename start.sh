#!/usr/bin/env bash
#
# GPA one-shot launcher (macOS).
#
# Usage:
#   ./start.sh                 # safe console: core deps, no visual preload/input
#   ./start.sh --enable-desktop # explicitly allow trusted Replay input
#   ./start.sh --visual        # install and preload optional visual models
#   ./start.sh --fast          # skip visual-model preload (legacy convenience)
#   ./start.sh --check         # validate setup without starting the server
#   ./start.sh --web           # use the legacy browser-hosted local console
#   ./start.sh --cli           # activate the env and open a shell
#   ./start.sh --skip-install  # don't touch pip; assume deps are already installed
#   ./start.sh --reinstall     # force-reinstall selected dependency groups
#   ./start.sh --no-visual     # explicit alias for the safe default
#
# Environment:
#   Reads ./.env if present (GPA_LLM_API_KEY / GPA_LLM_BASE_URL / GPA_LLM_MODEL).
#   Command-line safety flags override matching values from .env.
#
set -euo pipefail

# ── Resolve project root (directory of this script) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PORT=8765
ENV_FILE="${GPA_ENV_FILE:-$SCRIPT_DIR/.env}"

# ── Parse flags ─────────────────────────────────────────────────────────────
FAST=0
CLI=0
SKIP_INSTALL=0
REINSTALL=0
NO_VISUAL=0
WITH_VISUAL=0
ENABLE_DESKTOP=0
DESKTOP_STARTUP_DEFAULT=0
CHECK_ONLY=0
WEB_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --fast)         FAST=1 ;;
    --cli)          CLI=1 ;;
    --check)        CHECK_ONLY=1 ;;
    --web)          WEB_ONLY=1 ;;
    --enable-desktop) ENABLE_DESKTOP=1 ;;
    --visual)       WITH_VISUAL=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --reinstall)    REINSTALL=1 ;;
    --no-visual)    NO_VISUAL=1; WITH_VISUAL=0 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0 ;;
    *)
      echo "Unknown option: $arg (use --help)" >&2
      exit 2 ;;
  esac
done

echo "==> GPA launcher — project: $SCRIPT_DIR"

# ── OS sanity check ─────────────────────────────────────────────────────────
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "!! Warning: GPA desktop capture/control requires platform input APIs." >&2
  echo "!! You're on $(uname -s); recording/replay will not work here." >&2
fi

# ── Locate a Python 3 interpreter ───────────────────────────────────────────
PYTHON_BIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$cand")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "!! No python3 found on PATH. Install Python 3.9+ and retry." >&2
  exit 1
fi
echo "==> Using interpreter: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# ── Create / reuse virtualenv ───────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
  echo "==> Creating virtualenv at .venv"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "==> Virtualenv active: $VIRTUAL_ENV"

# ── Install dependencies ────────────────────────────────────────────────────
install_deps() {
  python -m pip install --upgrade pip >/dev/null
  if [[ "$WEB_ONLY" -eq 1 ]]; then
    echo "==> Installing core dependencies (pip install -e .)"
    python -m pip install -e .
  else
    echo "==> Installing desktop application dependencies"
    python -m pip install -e ".[desktop]"
  fi
  if [[ "$WITH_VISUAL" -eq 1 && "$NO_VISUAL" -eq 0 ]]; then
    echo "==> Installing visual extras (pip install -e '.[visual]') — this can be large/slow"
    python -m pip install -e ".[visual]"
  else
    echo "==> Visual extras not selected (use --visual)"
  fi
}

core_deps_ready() {
  # Video evidence is a core product contract, not a visual-model extra.  A
  # partially populated virtualenv must not start a server that silently marks
  # every uploaded recording as undecodable.
  python -c 'import gpa, cv2, yaml, numpy' >/dev/null 2>&1
}

visual_deps_ready() {
  python -c 'import torch, ultralytics, transformers, sentence_transformers' >/dev/null 2>&1
}

desktop_deps_ready() {
  python -c 'import webview' >/dev/null 2>&1
}

if [[ "$SKIP_INSTALL" -eq 1 ]]; then
  echo "==> Skipping dependency install (--skip-install)"
elif [[ "$REINSTALL" -eq 1 ]]; then
  echo "==> Force reinstalling dependencies (--reinstall)"
  install_deps
elif ! core_deps_ready; then
  echo "==> Core package or recording decoder is missing; installing dependencies"
  install_deps
elif [[ "$WEB_ONLY" -eq 0 ]] && ! desktop_deps_ready; then
  echo "==> Desktop shell is missing; installing it"
  install_deps
elif [[ "$WITH_VISUAL" -eq 1 && "$NO_VISUAL" -eq 0 ]] && ! visual_deps_ready; then
  echo "==> Optional visual dependencies are missing; installing them"
  install_deps
else
  echo "==> Dependencies already present (use --reinstall to force)"
fi

# Older GPA environments may still contain pynput from before the macOS
# recorder moved to raw Quartz events. Leaving that wheel installed lets stale
# helpers re-enter TextInputSources and abort Python. Remove it from this
# project-owned virtualenv during normal setup; --skip-install still leaves pip
# state untouched.
if [[ "$(uname -s)" == "Darwin" && "$SKIP_INSTALL" -eq 0 ]] \
  && python -m pip show pynput >/dev/null 2>&1; then
  echo "==> Removing unsafe legacy pynput package on macOS"
  python -m pip uninstall -y pynput >/dev/null
fi

# ── Load .env (if present) ──────────────────────────────────────────────────
unset GPA_DESKTOP_STARTUP_ENABLED
if [[ -f "$ENV_FILE" ]]; then
  echo "==> Loading environment from $ENV_FILE"
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
else
  echo "!! No .env found — LLM-assisted build/replay needs GPA_LLM_API_KEY."
  echo "   Copy .env.example to .env and fill it in if you hit LLM errors."
fi
PORT="${GPA_PORT:-8765}"
case "${GPA_DESKTOP_STARTUP_ENABLED:-0}" in
  1|true|TRUE|yes|YES|on|ON) DESKTOP_STARTUP_DEFAULT=1 ;;
  0|false|FALSE|no|NO|off|OFF|"") DESKTOP_STARTUP_DEFAULT=0 ;;
  *)
    echo "!! GPA_DESKTOP_STARTUP_ENABLED must be true or false; using safe default." >&2
    DESKTOP_STARTUP_DEFAULT=0 ;;
esac

# ── Explicit runtime safety policy ─────────────────────────────────────────
# CLI flags win over inherited values and .env so the launcher is safe by
# default even when a previous shell exported permissive settings.
if [[ "$ENABLE_DESKTOP" -eq 1 || "$DESKTOP_STARTUP_DEFAULT" -eq 1 ]]; then
  export GPA_ENABLE_DESKTOP_AUTOMATION=1
else
  export GPA_ENABLE_DESKTOP_AUTOMATION=0
fi
if [[ "$(uname -s)" == "Darwin" ]]; then
  # Avoid pynput's background TISCopyCurrentKeyboardInputSource path, which can
  # abort Python outright on macOS.  Quartz capture keeps raw keycodes and never
  # performs TextInputSources translation on the listener thread.
  export GPA_RECORDING_INPUT_BACKEND=quartz
else
  export GPA_RECORDING_INPUT_BACKEND=pynput
fi
if [[ "$WITH_VISUAL" -eq 1 && "$NO_VISUAL" -eq 0 && "$FAST" -eq 0 ]]; then
  export GPA_PRELOAD_VISUAL_MODELS=1
  export GPA_REQUIRE_VISUAL_WARMUP=1
else
  export GPA_PRELOAD_VISUAL_MODELS=0
  export GPA_REQUIRE_VISUAL_WARMUP=0
fi

if [[ "$GPA_ENABLE_DESKTOP_AUTOMATION" == "1" ]]; then
  if [[ "$ENABLE_DESKTOP" -eq 1 ]]; then
    echo "==> Desktop automation: ENABLED for trusted Replays (CLI opt-in)"
  else
    echo "==> Desktop automation: ENABLED for trusted Replays (saved device preference)"
  fi
else
  echo "==> Desktop automation: disabled (use --enable-desktop to opt in)"
fi
echo "==> Recording input backend: $GPA_RECORDING_INPUT_BACKEND"
if [[ "$GPA_PRELOAD_VISUAL_MODELS" == "1" ]]; then
  echo "==> Visual model preload: enabled"
else
  echo "==> Visual model preload: disabled (use --visual to opt in)"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  if ! core_deps_ready; then
    echo "!! Core dependencies are incomplete (gpa/cv2/yaml/numpy)." >&2
    echo "   Run ./start.sh once without --skip-install to repair the environment." >&2
    exit 1
  fi
  python -c 'import gpa, cv2; print("==> Core package + recording decoder: ready")'
  if desktop_deps_ready; then
    echo "==> Native desktop shell: ready"
  else
    echo "!! Native desktop shell not installed; normal startup will install it." >&2
  fi
  echo "==> Check complete; server was not started."
  exit 0
fi

# ── CLI mode: hand the user an activated shell ──────────────────────────────
if [[ "$CLI" -eq 1 ]]; then
  echo "==> CLI mode ready. Try:  gpa list   |   gpa record <name>   |   gpa run <name>"
  if [[ "$ENABLE_DESKTOP" -eq 1 ]]; then
    echo "    Desktop automation is enabled for this shell."
  else
    echo "    Desktop automation remains disabled."
  fi
  exec "${SHELL:-/bin/bash}"
fi

# ── macOS permission reminder ───────────────────────────────────────────────
if [[ "$(uname -s)" == "Darwin" && "$GPA_ENABLE_DESKTOP_AUTOMATION" == "1" ]]; then
  echo "==> Reminder: grant your terminal Accessibility + Input Monitoring"
  echo "   under System Settings → Privacy & Security, or capture/replay will fail."
fi

# ── Launch the desktop application or legacy browser console ───────────────
if [[ "$WEB_ONLY" -eq 1 ]]; then
  echo "==> Starting GPA web console on http://127.0.0.1:${PORT}"
  echo "   (Ctrl-C to stop; add --visual only when visual replay is needed)"
  exec gpa-web
fi

DESKTOP_PORT="${GPA_DESKTOP_PORT:-0}"
echo "==> Starting GPA desktop application"
echo "   (the interface stays local; cloud data uses configured GPA services)"
exec gpa-desktop --port "$DESKTOP_PORT"
