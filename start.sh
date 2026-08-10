#!/usr/bin/env bash
#
# GPA one-shot launcher (macOS).
#
# Usage:
#   ./start.sh                 # set up venv + deps, then start the web console
#   ./start.sh --fast          # skip visual-model preload for a quicker start
#   ./start.sh --cli           # just activate the env and drop into a shell with `gpa` ready
#   ./start.sh --skip-install  # don't touch pip; assume deps are already installed
#   ./start.sh --reinstall     # force-reinstall project deps (core + visual)
#   ./start.sh --no-visual     # install core deps only (skip the heavy [visual] extras)
#
# Environment:
#   Reads ./.env if present (GPA_LLM_API_KEY / GPA_LLM_BASE_URL / GPA_LLM_MODEL).
#   Forces GPA_ENABLE_DESKTOP_AUTOMATION=1 so replay can actually drive the desktop.
#
set -euo pipefail

# ── Resolve project root (directory of this script) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
PORT=8765

# ── Parse flags ─────────────────────────────────────────────────────────────
FAST=0
CLI=0
SKIP_INSTALL=0
REINSTALL=0
NO_VISUAL=0
for arg in "$@"; do
  case "$arg" in
    --fast)         FAST=1 ;;
    --cli)          CLI=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --reinstall)    REINSTALL=1 ;;
    --no-visual)    NO_VISUAL=1 ;;
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
  echo "!! Warning: GPA drives the macOS desktop via pyautogui/pynput/Quartz." >&2
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
  echo "==> Installing core dependencies (pip install -e .)"
  python -m pip install -e .
  if [[ "$NO_VISUAL" -eq 0 ]]; then
    echo "==> Installing visual extras (pip install -e '.[visual]') — this can be large/slow"
    python -m pip install -e ".[visual]"
  else
    echo "==> Skipping visual extras (--no-visual)"
  fi
}

if [[ "$SKIP_INSTALL" -eq 1 ]]; then
  echo "==> Skipping dependency install (--skip-install)"
elif [[ "$REINSTALL" -eq 1 ]]; then
  echo "==> Force reinstalling dependencies (--reinstall)"
  install_deps
elif ! python -c "import gpa" >/dev/null 2>&1; then
  echo "==> Project not importable yet; installing dependencies"
  install_deps
else
  echo "==> Dependencies already present (use --reinstall to force)"
fi

# ── Load .env (if present) ──────────────────────────────────────────────────
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  echo "==> Loading environment from .env"
  set -a
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.env"
  set +a
else
  echo "!! No .env found — LLM-assisted build/replay needs GPA_LLM_API_KEY."
  echo "   Copy .env.example to .env and fill it in if you hit LLM errors."
fi

# ── Required runtime env ────────────────────────────────────────────────────
# Without this, every desktop action is safety-blocked by design.
export GPA_ENABLE_DESKTOP_AUTOMATION=1
if [[ "$FAST" -eq 1 ]]; then
  echo "==> Fast mode: skipping visual-model preload"
  export GPA_PRELOAD_VISUAL_MODELS=0
  export GPA_REQUIRE_VISUAL_WARMUP=0
fi

# ── CLI mode: hand the user an activated shell ──────────────────────────────
if [[ "$CLI" -eq 1 ]]; then
  echo "==> CLI mode ready. Try:  gpa list   |   gpa record <name>   |   gpa run <name>"
  echo "    (GPA_ENABLE_DESKTOP_AUTOMATION=1 is already exported in this shell)"
  exec "${SHELL:-/bin/bash}"
fi

# ── macOS permission reminder ───────────────────────────────────────────────
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "==> Reminder: grant your terminal Accessibility + Input Monitoring"
  echo "   under System Settings → Privacy & Security, or capture/replay will fail."
fi

# ── Launch the web console ──────────────────────────────────────────────────
echo "==> Starting GPA web console on http://127.0.0.1:${PORT}"
echo "   (first run may download visual models and take a while; Ctrl-C to stop)"
exec python demo_web/server.py
