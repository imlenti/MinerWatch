#!/usr/bin/env bash
# MinerWatch launcher
# Creates/reuses a venv, installs dependencies, and launches uvicorn.
set -e

cd "$(dirname "$0")"

VENV_DIR=".venv"

# Pick a supported interpreter (3.10-3.13). MinerWatch's pinned deps
# (pydantic-core, cryptography) have no wheels for 3.14+, so creating the
# venv with the bare system `python3` can land on an unsupported version and
# break `pip install`. An explicit PYTHON_BIN env var still takes precedence.
find_supported_python() {
    local name dir
    for name in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return 0; fi
    done
    for name in python3.13 python3.12 python3.11 python3.10; do
        for dir in /opt/homebrew/bin /usr/local/bin \
                   /Library/Frameworks/Python.framework/Versions/*/bin; do
            [ -x "$dir/$name" ] && { echo "$dir/$name"; return 0; }
        done
    done
    return 1
}
PYTHON_BIN="${PYTHON_BIN:-$(find_supported_python || true)}"
if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: no supported Python (3.10-3.13) found."
    echo "Install one, e.g.:  brew install python@3.13"
    echo "or download 3.13 from https://www.python.org/downloads/"
    exit 1
fi

echo "[MinerWatch] Using Python: $($PYTHON_BIN --version 2>&1) ($(command -v $PYTHON_BIN))"

# Recreate the venv if it's incomplete (missing activate) OR was built with an
# unsupported Python version (e.g. a previous run that used 3.14).
if [ -d "$VENV_DIR" ]; then
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        echo "[MinerWatch] Existing venv is incomplete, recreating it."
        rm -rf "$VENV_DIR"
    elif ! "$VENV_DIR/bin/python" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)' >/dev/null 2>&1; then
        echo "[MinerWatch] Existing venv uses an unsupported Python, recreating it."
        rm -rf "$VENV_DIR"
    fi
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "[MinerWatch] Creating virtualenv in $VENV_DIR ..."
    if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
        echo
        echo "ERROR: '$PYTHON_BIN -m venv' failed."
        echo
        echo "On macOS, Apple's bundled Python sometimes has issues with the venv module."
        echo "Workarounds:"
        echo "  1. Install a supported Python (3.10-3.13):  brew install python@3.13"
        echo "     then re-run:  ./start.sh   (it auto-detects 3.13)"
        echo "  2. Or use pip --user directly:"
        echo "     pip3 install --user --break-system-packages -r requirements.txt"
        echo "     python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000"
        exit 1
    fi
    if [ ! -f "$VENV_DIR/bin/activate" ]; then
        echo "ERROR: venv was created but activate is missing. ensurepip is probably broken."
        echo "Try: brew install python   and then re-run."
        exit 1
    fi
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "[MinerWatch] Upgrading pip and installing dependencies ..."
python -m pip install --quiet --no-cache-dir --upgrade pip
python -m pip install --quiet --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Frontend bundle auto-heal
# ---------------------------------------------------------------------------
# The React SPA is built from frontend-react/ into frontend-react/dist/.
# That directory is a build artifact, not tracked by git: a fresh
# ``git clone`` produces no dist/, and a ``git pull`` after a frontend
# change leaves the *old* dist/ behind (mismatched against the new
# code). In both cases the user sees a blank page in the browser.
#
# Self-healing strategy:
#   1. Stamp ``frontend-react/dist/.built-version`` with the VERSION
#      it was built against (CI does this on every release; the
#      manual fallback below does too).
#   2. On every boot, compare that stamp against the current VERSION.
#      If missing or mismatched, fix dist/ in one of two ways:
#        a) Download the prebuilt tarball from the GitHub release
#           matching VERSION (no Node required — works on Pi OS Lite).
#        b) If the download fails AND Node is available locally,
#           rebuild from source.
#   3. If both fail, warn loudly but still launch uvicorn — the
#      backend keeps working, and ``_react_index_response`` already
#      returns a helpful 503 explaining the manual fix.
#
# Opt out with: MINERWATCH_SKIP_FRONTEND_AUTOHEAL=1 ./start.sh

DIST_DIR="frontend-react/dist"
DIST_INDEX="$DIST_DIR/index.html"
DIST_STAMP="$DIST_DIR/.built-version"
WANTED_VERSION="$(cat VERSION 2>/dev/null || echo 0.0.0)"
HAVE_VERSION="$(cat "$DIST_STAMP" 2>/dev/null || echo none)"

if [ "${MINERWATCH_SKIP_FRONTEND_AUTOHEAL:-0}" = "1" ]; then
    echo "[MinerWatch] Frontend auto-heal disabled by env var."
elif [ -f "$DIST_INDEX" ] && [ "$HAVE_VERSION" = "$WANTED_VERSION" ]; then
    echo "[MinerWatch] Frontend dist/ already at v$WANTED_VERSION — ok."
else
    if [ ! -f "$DIST_INDEX" ]; then
        echo "[MinerWatch] Frontend dist/ missing — installing..."
    else
        echo "[MinerWatch] Frontend dist/ version mismatch (have: $HAVE_VERSION, want: $WANTED_VERSION) — refreshing..."
    fi

    HEALED=false
    TMP_DIR="$(mktemp -d)"
    TARBALL_URL="https://github.com/imlenti/MinerWatch/releases/download/v${WANTED_VERSION}/minerwatch-${WANTED_VERSION}.tar.gz"

    # Strategy 1: download prebuilt dist from the matching GitHub Release.
    # Fast (~1.5 MB), no Node required, deterministic.
    echo "[MinerWatch] Downloading prebuilt frontend from $TARBALL_URL ..."
    if curl -fsSL --connect-timeout 10 --max-time 120 \
            -o "$TMP_DIR/mw.tar.gz" "$TARBALL_URL"; then
        if tar xzf "$TMP_DIR/mw.tar.gz" -C "$TMP_DIR" \
           && [ -f "$TMP_DIR/minerwatch-${WANTED_VERSION}/frontend-react/dist/index.html" ]; then
            mkdir -p "$DIST_DIR"
            # rsync if available (cleaner), else cp -R as a fallback for
            # minimal systems where rsync isn't installed.
            if command -v rsync >/dev/null 2>&1; then
                rsync -a --delete \
                    "$TMP_DIR/minerwatch-${WANTED_VERSION}/frontend-react/dist/" \
                    "$DIST_DIR/"
            else
                rm -rf "$DIST_DIR"/*
                cp -R "$TMP_DIR/minerwatch-${WANTED_VERSION}/frontend-react/dist/." \
                       "$DIST_DIR/"
            fi
            echo "$WANTED_VERSION" > "$DIST_STAMP"
            echo "[MinerWatch] ✓ Frontend installed from release v${WANTED_VERSION}"
            HEALED=true
        else
            echo "[MinerWatch] Tarball extraction failed or dist/index.html missing."
        fi
    else
        echo "[MinerWatch] Could not download release tarball (offline? release v${WANTED_VERSION} not published yet?)."
    fi
    rm -rf "$TMP_DIR"

    # Strategy 2 (fallback): build locally if Node is available.
    if ! $HEALED; then
        if command -v npm >/dev/null 2>&1 && [ -f frontend-react/package.json ]; then
            echo "[MinerWatch] Falling back to local build (npm install + npm run build)..."
            if (cd frontend-react && npm install --silent && npm run build); then
                echo "$WANTED_VERSION" > "$DIST_STAMP"
                echo "[MinerWatch] ✓ Frontend built locally"
                HEALED=true
            else
                echo "[MinerWatch] Local build failed."
            fi
        else
            echo "[MinerWatch] No Node.js available for a local rebuild."
        fi
    fi

    if ! $HEALED; then
        echo
        echo "WARNING: Could not heal frontend dist/. MinerWatch will start,"
        echo "but the web UI will respond with 503 until you either:"
        echo "  - install Node.js and run: cd frontend-react && npm install && npm run build"
        echo "  - or manually grab the release tarball from:"
        echo "    https://github.com/imlenti/MinerWatch/releases"
        echo
    fi
fi

# Data folder
mkdir -p data

HOST="${MINERWATCH_HOST:-0.0.0.0}"
PORT="${MINERWATCH_PORT:-8000}"

# Host LAN addresses, to print convenient URLs.
LAN_IPS=$(ifconfig 2>/dev/null | awk '/inet / && $2 != "127.0.0.1" {print $2}' | head -3)

echo
echo "============================================"
echo "  MinerWatch listening on $HOST:$PORT"
echo "  Local:  http://localhost:$PORT"
for ip in $LAN_IPS; do
    echo "  LAN:    http://$ip:$PORT"
done
echo "  Press Ctrl+C to stop"
echo "============================================"
echo

exec uvicorn backend.main:app --host "$HOST" --port "$PORT" --reload
