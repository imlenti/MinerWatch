#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# release-umbrel.sh - one-command "Update": ship a new MinerWatch version AND
# push it to the Umbrel Community App Store.
#
# Usage:
#   ./scripts/release-umbrel.sh X.Y.Z
#
# It does, in order:
#   1. Forces the imlenti identity in both repos.
#   2. Runs scripts/release.sh X.Y.Z --yes
#        bumps VERSION + frontend package files, commits vX.Y.Z, tags, pushes.
#        The tag push makes GitHub Actions build the GitHub Release AND publish
#        ghcr.io/imlenti/minerwatch:X.Y.Z + :latest. No manual package step.
#   3. Bumps the Umbrel manifests (umbrel/ and community-app-store/), commits,
#      pushes. These are not in the tag and do not need to be.
#   4. Waits for you to confirm the GHCR image is published.
#   5. Mirrors the canonical app folder into minerwatch-app-store/, commits,
#      pushes. Umbrel users now get X.Y.Z.
#
# The app store repo is assumed to be a sibling of this repo. Override with:
#   APP_STORE_DIR=/path/to/minerwatch-app-store ./scripts/release-umbrel.sh X.Y.Z

set -euo pipefail

GIT_NAME="imlenti"
GIT_EMAIL="280957457+imlenti@users.noreply.github.com"

V="${1:-}"
if ! printf '%s' "$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "Usage: $0 X.Y.Z   (e.g. $0 1.6.3)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MINERWATCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_STORE_DIR="${APP_STORE_DIR:-$(cd "$MINERWATCH_DIR/.." && pwd)/minerwatch-app-store}"

if [ ! -d "$APP_STORE_DIR/.git" ]; then
  echo "App store repo not found at: $APP_STORE_DIR" >&2
  echo "Set APP_STORE_DIR=/path/to/minerwatch-app-store and retry." >&2
  exit 1
fi

# Portable in-place sed: BSD/macOS needs -i '' , GNU/Linux needs -i .
if [ "$(uname)" = "Darwin" ]; then
  sed_inplace() { sed -i '' -E "$@"; }
else
  sed_inplace() { sed -i -E "$@"; }
fi

set_identity() {
  git -C "$1" config user.name "$GIT_NAME"
  git -C "$1" config user.email "$GIT_EMAIL"
}

echo "==> Update MinerWatch to $V"
echo "    source repo: $MINERWATCH_DIR"
echo "    store  repo: $APP_STORE_DIR"

set_identity "$MINERWATCH_DIR"
set_identity "$APP_STORE_DIR"

# ---- 1. source release + tag + image (reuses the existing script) ----
"$MINERWATCH_DIR/scripts/release.sh" "$V" --yes

# ---- 2. bump the Umbrel manifests ----
cd "$MINERWATCH_DIR"
sed_inplace "s/^version: .*/version: \"$V\"/" umbrel/umbrel-app.yml
sed_inplace "s/^version: .*/version: \"$V\"/" community-app-store/imlenti-minerwatch/umbrel-app.yml
sed_inplace "s#^( *image: ghcr.io/imlenti/minerwatch:).*#\\1$V#" umbrel/docker-compose.yml
sed_inplace "s#^( *image: ghcr.io/imlenti/minerwatch:).*#\\1$V#" community-app-store/imlenti-minerwatch/docker-compose.yml

git add umbrel/umbrel-app.yml umbrel/docker-compose.yml \
  community-app-store/imlenti-minerwatch/umbrel-app.yml \
  community-app-store/imlenti-minerwatch/docker-compose.yml
git commit -m "Umbrel manifests $V"
git push origin main

# ---- 3. wait for the image ----
echo
echo "==> Tag pushed. Build: https://github.com/imlenti/MinerWatch/actions"
printf "Press ENTER once ghcr.io/imlenti/minerwatch:%s is published, or Ctrl-C to stop. " "$V"
read -r _

# ---- 4. mirror into the store repo ----
cd "$APP_STORE_DIR"
cp "$MINERWATCH_DIR/community-app-store/imlenti-minerwatch/umbrel-app.yml" imlenti-minerwatch/umbrel-app.yml
cp "$MINERWATCH_DIR/community-app-store/imlenti-minerwatch/docker-compose.yml" imlenti-minerwatch/docker-compose.yml

git add imlenti-minerwatch/umbrel-app.yml imlenti-minerwatch/docker-compose.yml
git commit -m "Update MinerWatch app to $V"
git push origin main

echo
echo "==> Done. MinerWatch $V released and the Community App Store points at it."
