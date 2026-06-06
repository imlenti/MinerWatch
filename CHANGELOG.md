# Changelog

All notable changes to MinerWatch are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.10.4] — 2026-06-06

### Added

- **First-run security nudge.** When the dashboard is reachable on your network
  but the controls are not protected by a password, a banner now warns that
  anyone on the network can view *and control* your miners (change
  frequency/voltage, redirect pool payout), with a one-click link straight to
  the Security settings. It stays until you set a password.
- **Just-in-time auto-scan warning.** The first time you run an auto-scan on an
  unprotected install, a dialog explains the exposure and offers to set a
  password. You can explicitly opt out ("I don't want to protect my miners");
  the choice is remembered server-side so it does not ask again.
- **Dependency vulnerability scanning in CI.** `pip-audit` and `npm audit` run
  on every push and weekly as a non-blocking signal, and Dependabot opens weekly
  update PRs across the Python, npm, GitHub Actions and Docker dependencies.

### Fixed

- **Enabling a password no longer leaves the UI stuck.** A protected request
  that comes back 401 now redirects to the login page — and returns you where
  you were after signing in — instead of failing silently. Previously, setting a
  password could leave the dashboard unable to load until you manually went to
  `/login`.
- **Corrected a misleading comment** in `config.example.yaml`: the auth password
  is not auto-generated. With auth enabled and no password set, the app fails
  closed (every protected request returns 401) until you set one.

### Security

- **Tightened data-directory permissions.** The data directory is now locked to
  the owner (`0700`) and the VAPID private key to `0600`, so other local users
  on the same machine cannot read the stored secrets. Best-effort — filesystems
  that reject `chmod` (read-only or network mounts) are tolerated without
  crashing.

## [1.10.3] — 2026-06-05

### Added

- **Guardian: per-miner max temperature.** The temperature the Guardian backs
  off at is now an editable per-miner field (Advanced tab) instead of a fixed
  global threshold. You set a single "max" number; the recovery point is
  derived from it (max − the source's hysteresis deadband), so each miner can
  carry its own ceiling. Unset miners keep the global default, so existing
  setups are unchanged.
- **Guardian: choose the temperature source (VR or ASIC chip).** A per-miner
  toggle picks which sensor governs frequency. VR stays the default and the
  recommendation — nothing else governs it in a closed loop. Chip mode is
  available for setups that want it, with the caveat (surfaced in the UI) that
  the chip is already driven by the fan PID and the 75 °C overheat watchdog, so
  a chip-driven governor only bites once the fan is saturated; the API rejects a
  chip max at/above 75 °C so it can never shadow the hard watchdog.
- **Guardian: at-your-own-risk confirmation on enable.** Turning the Guardian on
  for a miner now opens a confirmation dialog noting it is an advanced feature
  used at the operator's own risk, recommending they stay near the miner the
  first time. Declining leaves the Guardian off. Disabling is unchanged (no
  prompt).

## [1.10.2] — 2026-06-05

### Fixed

- **Live shares dropped early for high-throughput miners.** The "All miners
  — live shares" chart evicted events with a per-miner *count* cap, so a
  multi-ASIC board like the SupraHex — which logs ASIC results several times
  faster than a single-ASIC Gamma — only retained ~5 minutes of history while
  slower miners spanned the whole window, making the Hex dots vanish from the
  left of the chart. Retention is now *time*-based (~15 min, covering the
  longest selectable range) on both the frontend hooks and the backend ring
  buffer, with a generous count cap kept only as a memory backstop.
- **Donations had no effect when a miner was on its fallback pool.** Starting
  a hashrate donation only rewrote the *primary* stratum slot, but a miner
  that had failed over to its fallback keeps mining the fallback after the
  restart (AxeOS persists `isUsingFallbackStratum`), so it never switched to
  the donation pool. The donation now detects the active slot and repoints
  whichever one the miner is actually mining on, leaving the other untouched;
  the automatic revert restores the full prior pool config as before.

## [1.6.0] — 2026-05-24

### Added

- **Published Docker image.** A new `docker-publish.yml` workflow builds and
  pushes a multi-arch (linux/amd64 + linux/arm64) image to
  `ghcr.io/imlenti/minerwatch` on every `vX.Y.Z` tag (and on manual dispatch).
  Docker, Umbrel and the Community App Store can now pull a prebuilt image
  instead of building from source.
- **Umbrel Community App Store package** under `community-app-store/`
  (`umbrel-app-store.yml` + `imlenti-minerwatch/`), installable by URL without
  going through the official store review.
- **Windows support via WSL2**, documented in the README — runs the standard
  Linux flow (`start.sh` / `install-service.sh`), including a working in-app
  update.
- **`container` flag** on `GET /api/version` so the UI can adapt its update
  guidance to the runtime.

### Changed

- **Container-aware updates.** Under Docker/Umbrel the in-app installer is now
  disabled (the image is immutable, so a file swap would be discarded on the
  next container recreate). `POST /api/update/install` returns 409, and the
  Update page keeps showing whether a newer release exists while pointing to
  `docker compose pull`. Bare-metal macOS/Linux self-update is unchanged.

### Fixed

- **Version reported as `0.0.0` under Docker.** The `VERSION` file is now
  copied into the image, so the footer, `/api/version` and the update check
  read the real version instead of falling back to `0.0.0` (which made the
  Update page permanently report an update available).

## [1.5.4] — 2026-05-24

### Fixed

- **Braiins BMM101 display name.** The BMM101 firmware doesn't report a model
  in its cgminer `version` payload, so discovery used to fall back to
  "Braiins `<ip>`" (e.g. `Braiins 192.168.1.12`). It now shows as
  **Braiins BMM101** on the dashboard and the miner tab, with the model field
  set to `BMM101`. Other miner families keep the host suffix to disambiguate
  identical models. Re-run a network scan to update an already-registered
  BMM101.

## [1.5.2] — 2026-05-24

### Fixed

- **Guardian instability signal** now uses the **rejected-share rate**
  (`sharesRejected / (accepted + rejected)`) instead of the AxeOS
  `hashrateMonitor` `errorCount / total`. The `total` field there turned out
  to be the ASIC *hashrate* (GH/s), not a work counter, so the old ratio
  produced absurd values (e.g. 478% / 7558%) and could throttle a perfectly
  cool, healthy miner down toward its floor every cycle. Reject rate is a
  genuine monotonic counter, in the right ballpark (well under 1% on a healthy
  miner) and available on every AxeOS family. A `reject_min_shares` guard
  (default 20) ignores intervals with too few shares so a single stale share
  can't spike the rate. Config: `hw_error_pct_max` → `reject_pct_max`.

## [1.5.0] — 2026-05-24

### Added

- **Guardian — a runtime frequency governor** (Bitaxe / Nerd*). A slow,
  always-on control loop (a twin of the auto-fan PID, but acting on ASIC
  *frequency* instead of the fan) that adapts to ambient heat. Per enabled
  miner it watches the VR temperature and the HW error rate and nudges
  frequency to keep both in bounds, recovering frequency when things cool —
  never above a per-miner **max frequency** ceiling (default: the current
  frequency, editable by expert users). It lives under the new miner
  **Advanced** tab. Because AxeOS applies frequency changes live, there is
  no reboot/downtime per nudge; the loop runs on a slow cadence (default
  5 min) sized to the VR's thermal settle time. v1 is frequency-only; a v2
  that also adjusts voltage is documented in `docs/guardian-design.md`.

### Removed

- **Efficiency/performance Tuner** (Performance / Eco profiles). Replaced by
  the Guardian above, which addresses day-to-day ambient drift that a static
  one-shot tuning point can't. The `tuner_sessions` / `tuner_points` tables
  are dropped automatically on the next start (idempotent migration).

## [1.1.6] — 2026-05-22

### Added

- **Both chip-temperature sensors on multi-ASIC Bitaxe boards**: the
  Hardware → Thermal section now shows "Chip temp 1" and "Chip temp 2"
  for boards that expose two on-board sensors (`temp` / `temp2`), such
  as the Bitaxe SupraHex (6× BM1368). Single-sensor boards are
  unchanged and keep the "Max chip temp" row.

### Fixed

- **Over-temperature alert and auto-fan now follow the hottest chip
  sensor** on multi-sensor Bitaxe boards. The driver previously fed only
  the first sensor (`temp`) into `temp_chip_c`, so the overheat alert
  and the auto-fan PID could ignore a hotter second cluster — on a
  SupraHex, sensor 2 can run well above sensor 1. `temp_chip_c` is now
  the maximum across all valid chip sensors, matching the LuxOS /
  Braiins / Canaan drivers; the firmware's `-1` "sensor absent" sentinel
  is excluded.

## [0.1.0] — 2026-05-10

First public alpha. Local-first dashboard for home Bitcoin miners,
covering Bitaxe / NerdQAxe (HTTP), Canaan Avalon Nano 3s / Avalon Q
(cgminer-text) and Braiins BMM 101 / BOSminer (cgminer-JSON with BOS
extensions).

### Added

- **Live dashboard** with fleet-wide hashrate, power, efficiency, max
  chip temp and per-miner cards (Chart.js graphs on the detail page).
- **Best-share tracker** — both *session* (since the miner's last
  reboot) and *all-time* (persisted in MinerWatch's DB) per miner and
  across the fleet, plus a dedicated push notification when a miner
  beats its own all-time record (with `+10 %` growth threshold and
  60 s per-miner cool-down to avoid spam).
- **Bitaxe NVS seeding**: on first contact with a Bitaxe / NerdQAxe,
  the firmware-persisted `bestDiff` is silently used to seed the
  all-time record so users don't lose history accumulated before
  installing MinerWatch.
- **Web Push notifications (VAPID)** for: chip / VR over-temperature,
  miner offline, miner recovered, and best-share records — with
  re-alerts every 600 s while a critical condition persists, plus a
  sticky "critical status" banner in the dashboard.
- **Auto-discovery** scanning the host's /24 (default `auto`) for
  ports 80 (Bitaxe-class) and 4028 (cgminer-class). Detected devices
  are MAC-pinned so DHCP lease changes don't break the time series.
- **Server-side auto-fan PID** controller mirroring the Bitaxe
  firmware (`Kp = 5`, `Ki = 0.1`, `Kd = 2`, P_ON_E, REVERSE, EMA
  α = 0.2, default target 60 °C). Sample period 10 s with automatic
  rescaling of the gains relative to the firmware's 100 ms loop.
- **Tiered SQLite retention** (raw → 1-minute → 1-hour) with a
  one-shot migration that backfills the rollup tables, prunes raw
  beyond `retention_raw_hours`, and `VACUUM`s to actually shrink the
  DB file.
- **macOS one-click installer** (`installer.command`) that copies
  MinerWatch to `~/Library/Application Support/MinerWatch/`,
  registers a LaunchAgent, and survives source folder moves /
  iCloud relocations.
- **systemd / launchd service installer** (`scripts/install-service.sh`)
  for headless Pi setups, with `enable-linger` instructions.
- **Docker compose** entry in the README (image building lands in a
  follow-up release).
- **Optional bearer-token auth** for setups where the LAN isn't fully
  trusted.
- **PDF reports** (optional, via WeasyPrint).
- AGPL-3.0 licensing with SPDX headers in every Python module.

### Drivers

- **Bitaxe / NerdQAxe**: HTTP REST on port 80, full read + control
  surface (`fan`, `frequency`, `coreVoltage`, `autofanspeed`,
  restart). Difficulty values parse SI strings (`"4.29G"`, `"2.15M"`)
  *and* numeric forms — older AxeOS releases, modern v2.x and
  forks all work.
- **Canaan Avalon Nano 3s / Avalon Q**: cgminer-text on port 4028
  with the Avalon dialect (`MM ID0` bracketed fields). Reads chip /
  VR temps, fans, frequency, accepted / rejected, best share. Writes
  fan speed (PWM 15-100 or `-1` for firmware auto), frequency,
  voltage and work mode. Power is read from `MPO[N]` (W).
- **Braiins BMM 101 / BOSminer**: cgminer-JSON on port 4028 plus
  Braiins extensions (`temps`, `fans`, `tunerstatus`) for chip-level
  temperatures and approximate chain power consumption.

### Quality / robustness

- LibreSSL-on-macOS workaround: VAPID private key fed to `pywebpush`
  in raw base64 instead of PEM, dodging the `header too long`
  parsing error.
- Service-worker push notifications use a unique `tag` per
  `(miner_id, timestamp)` so consecutive alerts don't merge.
- Auto-discovery returns `None` instead of silently falling back to
  `192.168.1.0/24` when the host's subnet can't be detected, with a
  clear log message that points users at the Settings page.

### Known issues

- Push notifications on macOS need *both* the Chrome per-site
  permission *and* the system-level notification permission for
  Chrome (System Settings → Notifications → Google Chrome).
- Braiins BMM firmwares older than the latest BOSminer build may
  return zeros for `temps` / `fans`; MinerWatch falls back gracefully
  but you'll see partial data.
- Canaan firmware refuses fan PWM values below 15 %; the driver maps
  anything `< 15` to firmware-auto (`-1`).
- No automated test suite yet — contributions welcome.

[Unreleased]: https://github.com/imlenti/MinerWatch/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/imlenti/MinerWatch/releases/tag/v1.6.0
[1.1.6]: https://github.com/imlenti/MinerWatch/releases/tag/v1.1.6
[0.1.0]: https://github.com/imlenti/MinerWatch/releases/tag/v0.1.0
