# MinerWatch — Umbrel app packaging

This folder contains everything Umbrel needs to install MinerWatch as
an app from the Umbrel App Store:

| File                | What it does                                              |
|---------------------|-----------------------------------------------------------|
| `umbrel-app.yml`    | App manifest (name, version, description, screenshots).  |
| `docker-compose.yml`| Service definition Umbrel runs on the user's machine.    |
| `1.jpg`–`3.jpg`     | Screenshots displayed in the App Store listing.          |
| `icon.svg`          | App icon (falls back to the repo `favicon.svg` if absent). |

The plain-Docker compose file at the repo root (`/docker-compose.yml`)
is unaffected. Run `docker compose up -d` from the root for a normal
self-hosted install; this folder is exclusively for the Umbrel store
submission.

## Why `network_mode: host`?

MinerWatch needs to scan the host's `/24` subnet to auto-discover
miners, and afterwards reach each miner directly on its LAN IP (e.g.
`192.168.1.7`). A bridge-networked container only sees Docker's
internal subnet and can't reach `192.168.x.x`, which breaks both
discovery and polling.

Umbrel's `app_proxy` still routes the app's UI normally on the manifest
port (8000); it just forwards to the host port `web` binds (8765) at
`$GATEWAY_IP`, the Umbrel host's address on its internal Docker network
(10.21.0.1), instead of an internal service IP. That address works both
when the proxy is a sidecar container (umbrelOS 1.x) and when umbreld
proxies the port itself (umbrelOS 2.0, which no longer creates the
`app_proxy` container). The user-facing experience (clicking "Open" in
the Umbrel dashboard) is identical to any other app.

The desktop widgets need one more hop: umbreld fetches widget data from
a container IP on Umbrel's Docker network, which a host-networked `web`
doesn't have (and umbrelOS 2.0 no longer creates an `app_proxy`
container to borrow one from). The small `widgets` service in
`docker-compose.yml`, a stdlib-only TCP relay running from the same
MinerWatch image, provides that IP and forwards to `$GATEWAY_IP:8765`.
The manifest's widget endpoints point at it (`widgets:8765/...`).

The price of host networking is one reserved port on the host: 8765.
If that port is already taken, change `MINERWATCH_PORT` in the
`environment` block and update `app_proxy.APP_PORT` to match.

## Publishing checklist

Before submitting to <https://github.com/getumbrel/umbrel-apps>:

1. **Tag and push the image**. Replace
   `ghcr.io/imlenti/minerwatch:1.6.0` in `docker-compose.yml` with the
   real registry path *and* pin to an immutable digest:

   ```bash
   docker buildx build \
     --platform linux/amd64,linux/arm64 \
     -t ghcr.io/imlenti/minerwatch:1.6.0 \
     --push .
   docker buildx imagetools inspect ghcr.io/imlenti/minerwatch:1.6.0
   # copy the digest and pin it: image: ghcr.io/imlenti/minerwatch:1.6.0@sha256:...
   ```

   The Umbrel App Store CI rejects un-pinned tags.

2. **Replace screenshots**. Drop three 1280×800 PNG/JPG screenshots
   into this folder named `1.jpg`, `2.jpg`, `3.jpg`. Good candidates:
   - Dashboard with miners, fleet hashrate chart, and Predictions card.
   - Miner detail page with the Hardware tab open (cards visible).
   - Settings → Notifications tab showing both Web Push and Telegram.

3. **Verify the manifest**. Open `umbrel-app.yml` and confirm:
   - `id` is unique across the App Store (currently `minerwatch`).
   - `version` matches the image tag.
   - `submission` points to a PR URL (filled in at submission time).

4. **Test locally with Umbrel CLI** (optional, recommended):

   ```bash
   git clone https://github.com/getumbrel/umbrel-apps.git
   cp -r path/to/MinerWatch/umbrel umbrel-apps/minerwatch
   cd umbrel-apps && bin/umbrel-cli test minerwatch
   ```

5. **Open the PR** against `getumbrel/umbrel-apps` with this folder as
   `minerwatch/`. Their CI will run the standard checks (manifest
   schema, image existence, port collision, etc.).

## After the app is live

Updates are shipped by bumping `version` in `umbrel-app.yml`, updating
the image tag+digest in `docker-compose.yml`, and opening another PR.
Umbrel pushes the update to every installed instance.

Persistent data lives in `${APP_DATA_DIR}/data` on the user's machine
— that's the SQLite database, VAPID keys, push subscriptions, and
config overrides. Image updates never wipe it.
