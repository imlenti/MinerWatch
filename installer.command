#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# installer.command — macOS one-click installer for MinerWatch.
#
# Double-clicking this file in Finder opens Terminal, deploys MinerWatch
# to ~/Library/Application Support/MinerWatch/, registers the LaunchAgent,
# starts the service, and opens the dashboard in your default browser.
#
# The runtime location is fixed because macOS Privacy (TCC) blocks
# background launchd jobs from reading Desktop / Documents / Downloads /
# iCloud Drive. By installing the running copy under
# ~/Library/Application Support, the LaunchAgent works regardless of
# where the user keeps the source folder.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$HOME/Library/Application Support/MinerWatch"

# MinerWatch's pinned dependencies (pydantic-core, cryptography) only publish
# prebuilt wheels for CPython 3.10-3.13. On Python 3.14+ pip falls back to
# compiling them from source, which needs a Rust toolchain and a newer pyo3
# than those releases ship - and fails. So we pick the newest *supported*
# interpreter we can find instead of trusting the bare `python3`.
SUPPORTED_PY=(python3.13 python3.12 python3.11 python3.10)
find_supported_python() {
    local name dir
    for name in "${SUPPORTED_PY[@]}"; do
        if command -v "$name" >/dev/null 2>&1; then
            command -v "$name"
            return 0
        fi
    done
    # Fallback: probe common install locations that may not be on PATH.
    for name in "${SUPPORTED_PY[@]}"; do
        for dir in /opt/homebrew/bin /usr/local/bin \
                   /Library/Frameworks/Python.framework/Versions/*/bin; do
            [[ -x "$dir/$name" ]] && { echo "$dir/$name"; return 0; }
        done
    done
    return 1
}

if [[ -t 1 ]]; then
    BOLD=$(tput bold); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); RESET=$(tput sgr0)
else
    BOLD=""; GREEN=""; YELLOW=""; RESET=""
fi

cat <<EOF
${BOLD}MinerWatch installer${RESET}
─────────────────────────────────────

This will:
  1. Copy MinerWatch into:
       ${RUNTIME_DIR}
  2. Create a Python virtual environment in that folder
  3. Install dependencies from requirements.txt
  4. Register MinerWatch as a macOS LaunchAgent (auto-start at login)
  5. Open the dashboard in your browser

Source folder (just for the copy):
  ${SOURCE_DIR}

The service runs from the runtime location, so privacy restrictions on
Desktop / Documents / Downloads / iCloud Drive can't break auto-start.

Press Enter to continue, or Ctrl-C to cancel.
EOF
read -r _

if ! PYTHON_BIN="$(find_supported_python)"; then
    echo "${YELLOW}No supported Python found.${RESET}" >&2
    echo "MinerWatch needs Python 3.10-3.13 (3.14+ isn't supported yet by its" >&2
    echo "dependencies). Install one and re-run this installer:" >&2
    echo "    brew install python@3.13" >&2
    echo "    # or download 3.13 from https://www.python.org/downloads/" >&2
    exit 1
fi

# 1. Deploy: rsync source -> runtime, excluding dev artifacts.
echo
echo "${BOLD}→ Deploying MinerWatch to runtime directory...${RESET}"
mkdir -p "$RUNTIME_DIR"
rsync -a --delete \
    --exclude='.venv/' \
    --exclude='data/' \
    --exclude='__pycache__/' \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    --exclude='HANDOFF.md' \
    --exclude='reports/' \
    "$SOURCE_DIR/" "$RUNTIME_DIR/"

# Strip macOS quarantine flag in case the source came from a download
# or was moved across TCC boundaries.
xattr -dr com.apple.quarantine "$RUNTIME_DIR" 2>/dev/null || true
chmod +x "$RUNTIME_DIR/start.sh" "$RUNTIME_DIR/stop.sh" \
         "$RUNTIME_DIR/installer.command" "$RUNTIME_DIR/uninstaller.command" \
         "$RUNTIME_DIR/scripts/install-service.sh" \
         "$RUNTIME_DIR/scripts/uninstall-service.sh" 2>/dev/null || true
echo "${GREEN}✓ Files synced${RESET}"

# 2-3. venv + deps in the runtime dir.
cd "$RUNTIME_DIR"

echo
echo "${BOLD}→ Setting up Python virtual environment...${RESET}"
echo "  Using $("$PYTHON_BIN" --version 2>&1) ($PYTHON_BIN)"
# Rebuild the venv if it's incomplete or was created with an unsupported
# Python (e.g. an earlier run that picked up the system's 3.14).
if [[ -d .venv ]] && ! .venv/bin/python -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1; then
    echo "  Existing .venv uses an unsupported/incomplete Python — rebuilding it."
    rm -rf .venv
fi
if [[ ! -d .venv ]]; then
    "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --no-cache-dir --upgrade pip
pip install --quiet --no-cache-dir -r requirements.txt
echo "${GREEN}✓ Virtual environment ready${RESET}"

# 4. install service from the runtime dir
echo
echo "${BOLD}→ Registering LaunchAgent...${RESET}"
./scripts/install-service.sh

# 5. open browser
PORT="$(grep -E '^\s*port:' config.example.yaml 2>/dev/null | head -1 | awk '{print $2}')"
PORT="${PORT:-8000}"
URL="http://localhost:$PORT"

echo
echo "${BOLD}→ Opening $URL${RESET}"
open "$URL" || true

cat <<EOF

${GREEN}${BOLD}All done!${RESET}

MinerWatch is installed in:
  ${RUNTIME_DIR}

It will start automatically every time you log in. You can move or
delete the source folder; the service will keep working.

To stop or remove the service, double-click ${BOLD}uninstaller.command${RESET}.

Press Enter to close this window.
EOF
read -r _
