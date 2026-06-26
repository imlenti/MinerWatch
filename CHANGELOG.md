# Changelog

All notable changes to MinerWatch are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.19.5] — 2026-06-29

### Fixed

- **MinerWatch no longer keeps the full miner payload on every metrics row.**
  Each poll used to store the miner's entire raw JSON response in a column of
  the `metrics` table. On an active fleet that single column grew to hundreds
  of megabytes inside the 48-hour raw-retention window, and the kernel held it
  all in page cache — which is what made the app's memory look alarmingly large
  on Umbrel even though almost none of it was actually in use. The raw payload
  is now stored only as the most recent sample per miner, in a small
  `latest_raw` table — the only place it was ever read (the
  `/api/miners/{id}/raw` debug endpoint) — so the per-row blob is gone and the
  database, and the cache behind it, shrink by an order of magnitude. Old rows
  clear themselves within the 48-hour window; a one-off `VACUUM` reclaims the
  file immediately. No API responses change, so the Monolith, Halo and ambient
  sensor integrations are unaffected.
- **Live-share streams no longer leak socket memory over long uptimes.** A
  miner detail view opens a Server-Sent Events stream for its live shares. If
  the browser went away without a clean disconnect — common behind a reverse
  proxy that doesn't propagate the close upstream — the connection and the
  socket buffers held for it stayed pinned until the process restarted, so
  memory crept upward the longer MinerWatch ran. Streams now notice a dropped
  client and, as a hard backstop, recycle on a capped lifetime (the browser
  reconnects on its own, so it is invisible), and the snapshot sent on connect
  is bounded. Memory stays flat across long uptimes instead of climbing toward
  a restart.

## [1.19.4] — 2026-06-27

### Fixed

- **Ambient temperatures sit on one line on desktop.** The room-temperature
  readout now stays on a single line at desktop widths and wraps to two lines
  only on narrow mobile screens, keeping the layout tidy on larger displays.

## [1.19.3] — 2026-06-27

### Fixed

- **Mobile layout fixes for ambient temperatures and the miner tab bar.** Room
  temperatures stack onto two lines so they fit narrow screens, the miner tab
  bar fits on mobile without overflowing, and each reading stays joined to its
  unit so a value and its unit never wrap apart.

## [1.19.2] — 2026-06-27

### Added

- **Guardian windowed metrics averaging.** Added windowed averaging of stochastic/noisy metrics (hashrate, temperature, and power) inside the Guardian governor loop, preventing false-positive throttling from transient dips or peaks. The averaging window can be configured via a new global setting in the general tab (defaults to 30 seconds, set to 0 to disable).
- **NMAxe miner family (NMAxe / NMAxeGamma / NMQAxe++).** Support for the NMAxe
  AxeOS fork, whose REST surface is fully nested — `GET /api/system/info` groups
  `power` / `temps` / `asic` / `miner` / `identity` / `stratum` / `fans[]` — with
  controls under `/api/setting/*` and no MAC. A new `nmaxe` driver reads the full
  metric set and drives the fan (auto + manual duty via
  `PATCH /api/setting/preference`) and restart; frequency/voltage, the Guardian
  co-tuner, pause/shutdown and pool repoint (donate) are intentionally off for
  this family. Auto-detection fingerprints the board via `GET /probe` ahead of
  the Bitaxe path, so an NMQAxe++ is no longer mis-classified as nerdoctaxe, and
  the miner is keyed on its host since NMAxe exposes no MAC (use a static IP /
  DHCP reservation). Live per-share streaming works over `ws://<ip>/ws` with a
  dedicated parser, and the last-share difficulty is also read from the poll
  (`miner.lastDiff`) so the Halo shows it even before the stream connects.
- **Synchronised History tooltips and a temperature legend.** Hovering any of
  the three History charts (Hashrate, Temperature, Reject rate) now highlights
  the same moment on all of them at once, with a dashed guide line under the
  cursor and a labelled point on each series. The Temperature chart also gains
  a compact legend (Chip / VR / Room) so its lines are readable at a glance
  without hovering.

### Changed

- **Browser push notifications hidden and disabled by default.** Native Web
  Push (VAPID) is being retired in favour of the Telegram channel, which works
  on any device without HTTPS and needs no browser left open. The "Browser push
  notifications" card is now hidden from Settings and the channel no longer
  sends, while all of its code, stored subscriptions and VAPID keys are kept in
  place so the feature can be restored at once if there is demand. Telegram is
  the recommended notification channel — set it up under Settings →
  Notifications.

### Fixed

- **History charts no longer flash on every refresh.** The per-miner History
  charts used to rebuild from scratch on each 5s poll — blinking through a
  loading skeleton and dropping whatever value you were hovering. The visible
  time window is now held steady between refreshes, and the previous data stays
  on screen while the next range loads, so the charts keep their place, the
  skeleton only appears on the very first load, and a refresh that lands while
  you are reading a tooltip no longer steals it. Auto-refresh also pauses while
  the pointer is over a chart and snaps back to live when it leaves.

## [1.19.1] — 2026-06-22

### Changed

- **Panel feed `GET /api/panel` now carries every ambient sensor (breaking).**
  It returns a `temps` array — one entry per live sensor, `{n, c, mn, mx}` (name,
  current, session min/max), capped and omitted entirely when no sensor is live —
  instead of the single `temp_c`/`temp_min_c`/`temp_max_c` of one primary sensor.
  The Monolith panel firmware must be updated to match: the bottom row rotates
  through the sensors (name + current, tap to advance) and a new temperatures
  page lists each room's current/min/max (hold the row to open). A sensor
  MinerWatch evicts after 5 minutes of silence drops off the panel on its own.

## [1.19.0] — 2026-06-21

### Added

- **Official ESP32-C3 ambient sensors (multi-sensor).** `POST /api/ambient`
  now follows the official sensor's contract — each device sends
  `{temp_c, name, sensor_id}` every ~5s — and MinerWatch keeps one rolling
  series per `sensor_id` (60s average, session min/max, freshness). The
  dashboard shows one row per sensor (`Name | Temperature | Min | Max`), with a
  per-sensor cap and stale eviction so the list stays bounded and self-cleaning.
- **Per-sensor ambient history.** The `ambient_metrics` raw and rollup tables
  are keyed by `sensor_id`, so each room sensor keeps its own 1h–30d series.
- **Per-miner room assignment.** A miner can be assigned to a room sensor
  (`POST /api/miners/{id}/ambient-sensor`); its History "Temperature" chart then
  overlays that sensor's series (none assigned → no ambient line), and the
  dashboard miner card shows the assigned room name next to the model.

### Changed

- **`POST /api/ambient` is now strict (breaking).** It requires `temp_c`
  (a finite number in −50…125 °C), `name` (1–40 characters after trimming) and
  `sensor_id` (exactly 12 lowercase hex digits), and rejects unknown or legacy
  fields — including the old `status` — with HTTP 422. There is no
  compatibility with earlier publishers that sent only `temp_c`.
- **`GET /api/fleet/ambient_temp` now returns a list (breaking).** The response
  is `{"sensors": [...]}` with one entry per live sensor (each carrying `name`
  and `sensor_id`) instead of a single object; at cold start the list is empty.

### Migration

- On first start the ambient history tables are reset to the per-sensor schema.
  The previous single-series ambient history is discarded (it is low-value and
  fleet-wide); per-miner metrics are not touched.

## [1.18.3] — 2026-06-21

### Added

- **HTTP panel feed.** New read-only `GET /api/panel` endpoint serves the
  consolidated fleet snapshot the external ESP32 touch panel renders (built by
  the pure `backend/panel.py`), plus `POST /api/ambient` to push room
  temperature from an external sensor. Both are LAN-trust / auth-exempt like
  `/api/halo`.

### Removed

- **MQTT publisher and Home Assistant integration.** The panel now reads its
  data over HTTP instead of an MQTT broker, and ambient temperature is pushed
  via `POST /api/ambient` instead of an MQTT topic. The `mqtt.*` settings, the
  Settings → MQTT tab, the `aiomqtt` dependency and the Home Assistant
  MQTT-discovery support are gone.

## [1.18.2] — 2026-06-20

### Added

- **Halo Bitcoin price.**

## [1.18.1] — 2026-06-20

### Changed

- **Halo live share updates.**

## [1.18.0] — 2026-06-20

### Added

- **Halo first implementation.**

- **Set the overheat-watchdog temperature per Avalon miner — instead of a fixed
  75°C for everyone.** The server-side overheat watchdog (which forces the fan to
  100% and sends an alert when chip temperature stays above a hard trigger,
  regardless of fan mode) has always used a fixed 75°C net. Avalon/Canaan miners
  can now override that trigger per device from the miner's **Advanced** tab: the
  default stays 75°C, but it can be set anywhere in 60–95°C for boards that run
  hotter by design. The release point — when the fan is handed back — trails the
  trigger by a fixed 10°C, so the hysteresis band moves with the setting and can
  never invert (the stock 75°C → 65°C behaviour is unchanged). The override is
  Avalon-only by design: every other family keeps the fixed 75°C net, so the
  Guardian's chip-mode guard and the copy that reference it stay correct. New
  per-miner `watchdog_overheat_c` column (NULL → the 75°C default), carried on
  the existing `POST /api/miners/{id}/control/fan_config` endpoint with a 60–95°C
  range check and a family guard that rejects the field on non-Avalon miners.

### Changed

- **Block-found notification copy is now coin-agnostic — "solved a block"
  instead of "solved a Bitcoin block".** MinerWatch is increasingly used to
  solo-mine SHA-256 coins other than Bitcoin (e.g. DigiByte), where a
  notification announcing a "Bitcoin block" was misleading and alarming — one
  user who hit a DigiByte block was startled by the wording. The push, Telegram
  and stored-alert body now read "solved a block"; the celebratory title
  ("🎉🎉 BLOCK FOUND!") and the dashboard/Umbrel block-find widgets were already
  generic.

## [1.16.0] — 2026-06-14

### Added

- **Mute a miner's offline alerts until it comes back — for when you power one
  down on purpose.** A miner switched off intentionally used to keep firing the
  disconnect alert every `repeat_seconds` (10 min by default) until it was turned
  back on, possibly weeks later. It can now be silenced: the first offline alert
  still fires (so a real, unexpected drop is never hidden), and a Mute control
  stops every repeat after it — no push, no Telegram, and the stale offline rows
  are acknowledged out of the unread banner so the silence is complete. The mute
  is a per-miner flag persisted in the database, so it survives a MinerWatch
  restart, and it clears itself automatically the next time the miner is polled
  online again — re-powering the device re-arms the alert, with no manual
  un-mute to remember. It is reachable from the dashboard alerts banner: a quick
  Mute on the collapsed bar when the latest alert is an offline one, plus a
  per-row Mute in the expanded list when several miners are down at once. A
  "muted" badge next to the offline status on the miner card keeps the silenced
  state visible at a glance. New backend endpoint
  `POST /api/miners/{id}/offline-mute` (sets the flag and acknowledges that
  miner's outstanding offline alerts) and a new `offline_muted` column on the
  `miners` table.

- **Remote standby — stop a miner and bring it back — for AxeOS and NerdQAxe.**
  A miner can now be put into standby straight from MinerWatch: the ASIC is
  powered down and held idle so power draw and heat fall to near nothing, while
  the controller stays online and reachable; a matching action resumes it. The
  state is non-persistent by design — a power cycle (or a restart) resumes
  mining — so a deliberately stopped miner never gets stuck off. Two firmware
  paths are wired up: AxeOS Bitaxe uses the soft `POST /api/system/pause` +
  `/resume` (no reboot; requires AxeOS ≥ v2.14.0b1, which first shipped the
  endpoints and the `miningPaused` flag), while NerdQAxe / NerdOctaxe (shufps
  firmware) use `POST /api/system/shutdown` and resume via a restart, since that
  firmware has no soft resume. The control is self-detecting: the "Standby"
  button appears only when the miner actually reports the state (`miningPaused`
  / `shutdown` in `/api/system/info`), so it never offers a control that older
  firmware would reject. New backend endpoints
  `POST /api/miners/{id}/control/pause|resume|shutdown`, `pause` / `shutdown`
  capability flags, and a `mining_paused` field on the live sample and the fleet
  list. A standby miner is skipped by the Guardian co-tuner and does not trip
  offline / zero-hashrate alerts. In the UI the control is a
  four-state machine — Mining, Pausing, Paused, Resuming — that polls the miner
  to completion instead of trusting the immediate command response: a pause is
  confirmed once hashing actually stops, a resume once the first new share is
  accepted (the firmware's reported hashrate is unreliable for a while after an
  ASIC re-init, so it is deliberately not used as the resume signal). A
  "Standby" badge shows on the miner page and a "standby" state replaces
  "online" on the dashboard cards. BitForge (forge-os) is now supported on
  firmware builds that add the endpoints — stock forge-os still has none — with
  the capability detected per device from the `miningPaused` field in
  `/api/system/info`, so a stock board keeps the button hidden while a custom
  build that exposes pause/resume gets it.

### Changed

- **The dashboard total hashrate scales its units.** The top-of-page "Total
  hashrate" KPI now shows a single decimal once the fleet exceeds 100 TH/s
  (e.g. 110.2 TH/s) and switches to PH/s at 1000 TH/s and above, instead of
  always printing TH/s with two decimals. Per-miner figures are unchanged.

### Fixed

- **An implausible hashrate right after a resume or restart is dropped.**
  AxeOS-family firmwares re-seed their hashrate estimator when the ASIC chain
  re-initialises (resuming from standby, or a restart) and for a while report a
  wildly inflated value that decays over minutes — a BitForge resume was
  observed spiking to hundreds of PH/s. MinerWatch now treats any reading above
  three times the chip's theoretical maximum (frequency × cores × ASICs) as a
  transient artefact and drops it, so it never reaches the time-series database,
  the fleet hashrate total, the efficiency figure or the Guardian co-tuner; the
  value returns on its own once the estimator settles.

- **Live shares keep working on axeOS 2.14.** The per-share log parser now
  reads a pool target printed in scientific notation: axeOS 2.14 switched the
  target format to `%g`, so a high vardiff comes out as e.g. `1.04858e+06`. The
  old number token stopped at the exponent and read `1.04858`, which collapsed
  the dashed pool-difficulty line and mislabelled almost every share as
  submitted. The parser also recognises the accepted/rejected verdict under its
  renamed 2.14 tag `stratum_v1_task`; the earlier `stratum_task` (axeOS ≤2.13
  and forge-os) and NerdQAxe's `stratum task (Pri/Sec)` stay supported. axeOS
  2.13 output (integer `%ld` target) parses exactly as before. Verified against
  the bitaxeorg/ESP-Miner sources at v2.13.1, v2.14.0 and master.

- **Each miner gets a distinct colour on the all-miners live-shares chart.**
  Colours were derived by hashing the miner id into a 12-colour palette, which
  reduced to `id % 12`, so any two miners whose ids were congruent mod 12 — for
  example a Gamma on id 3 and a BitForge on id 15 — were drawn in the same hue.
  The palette is now allocated across the whole fleet as a set: each miner keeps
  its natural slot when free and otherwise takes the next free one, so up to
  twelve miners stay visually distinct, and a miner's colour holds steady as
  others are toggled on or off.

- **The all-miners live-shares chart shows a realistic per-miner hashrate, or
  "—" when it cannot tell.** The legend's TH/s figure is estimated from
  submitted shares; the previous method divided each miner's summed share work
  by the span between its first and last share in a 60-second window, which
  systematically overestimated (roughly by N/(N-1)) and spiked to implausible
  values whenever only a couple of shares landed close together. It now sums a
  fixed count of the most recent submitted shares (25) and divides by the time
  from the oldest of those shares to now, which removes the bias and keeps the
  statistical error roughly constant whatever the pool's vardiff. When fewer
  than that many submitted shares are available within the retention window it
  shows "—" instead of a fabricated number, and a stalled miner decays to "—"
  rather than freezing on its last value.

## [1.15.1] — 2026-06-13

### Changed

- **System (host metrics) page hidden for now.** It is hidden on every host,
  including Raspberry Pi. The page reads cleanly on a Pi but is unreliable
  elsewhere — in particular inside the Umbrel container, where limited `/sys`
  access, the container overlay filesystem reported as "disk", and the absence
  of `vcgencmd` make the readings misleading. The page, its host-capability
  detection (`supported` flag on `GET /api/system/info`) and the backend
  endpoints all stay in the code behind a single `SYSTEM_PAGE_ENABLED` switch,
  ready to re-enable once it works reliably across platforms (the 1.15.0
  attempt below regressed on Umbrel).

## [1.15.0] — 2026-06-13

### Added

- **Customizable dashboard layout.** The main dashboard's movable cards —
  fleet summary, ambient temperature, best shares, the hashrate chart and the
  miner grid — can now be reordered. A new "Customize layout" mode, entered
  from Settings → General, swaps those sections for compact draggable chips, so
  the full-height cards (and the miner grid's own drag-and-drop) are not mounted
  while arranging and there is no nested drag-and-drop; drag to reorder, then
  "Done" applies it. The order is persisted server-side (new
  `GET`/`POST`/`DELETE /api/dashboard/layout`, settings key
  `_dashboard_section_order`) so it follows the operator across browsers, and
  self-heals when sections are added or removed. The toolbar, alert banners and
  the block-find trophy stay pinned at the top; "Reset to default" clears the
  custom order. Reordering the individual miner cards within the grid is
  unchanged.

- **Author credit in the sidebar footer.** The sidebar and mobile-drawer footer
  now show a "₿uilt by Lenti" link — opening the author's X profile (@imlenti)
  in a new tab — above the existing version and "No cloud · AGPL-3.0" lines.

### Changed

- **System page shown on any sensor-capable Linux host, not just Raspberry
  Pi.** The "System" sidebar entry and page appeared whenever the host was
  Linux and exposed at least one hardware signal — a CPU temperature sensor
  (`/sys/class/thermal` or `vcgencmd`) or a discoverable fan — instead of being
  gated on Raspberry Pi detection. Introduced a `supported` flag on
  `GET /api/system/info`. (Reverted in 1.15.1: it regressed inside the Umbrel
  container.)

## [1.14.1] — 2026-06-13

### Added

- **Room temperature overlaid on each miner's History "Temperature" chart.**
  The ambient value relayed from the optional MQTT sensor — previously only
  shown live (dashboard card + ESP32 panel) — is now stored as a fleet-wide
  time-series and drawn as a third, dashed line on every miner's History
  temperature chart, alongside chip and VR. It is persisted once per poll
  cycle (when a fresh reading is available) and rolled up into the same
  1-minute and 1-hour tiers as the per-miner metrics, with matching tiered
  retention, so the room line lines up with the chip/VR series across the
  full 1h–30d range selector. New endpoint `GET
  /api/fleet/ambient_temp/history`. The line appears only when the relay has
  stored data; setups without an ambient sensor are unaffected.

- **Live shares survive forge-os v1.5 (synthetic share events).** forge-os
  v1.5 demotes the per-share `asic_result` log line to DEBUG and stock
  builds compile it out, so the BitForge Nano's live chart went dark after
  the firmware update. The streamer now reconstructs submitted shares from
  the pool verdict lines that are still logged at INFO: each
  accepted/rejected verdict becomes one share event plotted at the pool
  target (a floor for the real difficulty, marked `estimated` and shown as
  "≥" in the tooltip). The target is tracked from the vardiff lines plus
  the REST `stratumDiff`, and when the poller observes a new
  `bestSessionDiff` the latest synthetic dot is upgraded to the exact
  difficulty (new `amend` SSE event) and fed to the near-block Hall of
  Fame. The fallback engages per-stream and automatically; a real
  `asic_result` line (v1.0 boards, custom builds with DEBUG logs) switches
  back to the full-fidelity path. Only the below-target cloud is lost on
  such firmware — the firmware simply never emits it.

### Changed

- **"All miners" is the default live-shares view, on a 10-minute window.**
  The Live shares page now opens on the fleet-wide chart (tab order
  matches), and the fleet chart defaults to the 10m range — on a vardiff'd
  solo pool, shorter windows often looked empty. A previously chosen tab
  still wins via localStorage.

- **Dashboard fleet KPI relabelled "Avg efficiency".** The efficiency tile
  at the top of the dashboard is the fleet-wide average (total power ÷
  total hashrate); the label now reads "Avg efficiency" instead of the bare
  "Efficiency", so it isn't mistaken for a single-miner figure.

### Fixed

- **Both fans shown for NerdQAxe / NerdOctaxe, with corrected connector
  labels.** The Hardware tab listed only the primary fan on dual-fan
  boards; it now shows the second (Aux/VRM) fan as well, matching the
  Overview tab. The NerdQAxe fan connectors are relabelled to their
  silkscreen names — M2 (lower) / M1 (upper) instead of C2/C1. NerdOctaxe,
  wired differently, keeps C2/C1.

- **Best-share notifications died on forge-os v1.5's numeric `bestDiff`.**
  v1.5 reports `bestDiff` as a full-precision number while
  `bestSessionDiff` stays an SI string quantized to 3 significant digits,
  so a freshly broken record arrived as two "different" values (field
  case: `1882611` vs `"1.88M"`). The strict `live >= hint` test in
  `update_best_records` then routed every record whose 3-digit rendering
  rounds down into the silent-seed path — roughly half of all new records
  never pushed. Values that agree within a 1% quantization tolerance are
  now treated as the same share, the full-precision number wins as the
  stored value, and a bump that is invisible at 3-digit display resolution
  (pure precision catch-up after the firmware switch) stays silent instead
  of pushing "new best 1.88 M (was 1.88 M)".
- **v1.5 share-log lines were unparseable even when present.** The target
  format changed from `of 1497.` to `of 1497.00.` and the old greedy
  regex captured the sentence dot too, so `float()` raised and the line
  was silently dropped. The number pattern now owns at most one decimal
  point (all dialects covered by tests), relevant for custom builds that
  re-enable the per-share line.
- **Pool repointing now round-trips forge-os v1.5's stratum TLS flags.**
  `read_pool_config`/`set_pool` carry `stratumTLS`/`stratumCert` (and the
  fallback twins): donate-hashrate explicitly writes `tls=0` so a slot
  previously configured for a TLS pool can't try TLS against the
  plain-TCP donation pool, and a revert restores the snapshot's own
  flags. Pre-TLS snapshots leave the firmware values untouched; older
  firmware drops the unknown keys.
- **Boards reporting `deviceModel: "invalid"` no longer get renamed.**
  forge-os reads the model from NVS with the literal default "invalid";
  a factory-v1.0 Nano OTA'd to v1.5 whose NVS never got the key would
  have been re-discovered as model "Invalid". The value is now treated
  as absent and the chiptemp/ASIC fingerprints decide, keeping
  "BitForge Nano".

## [1.14.0] — 2026-06-12

### Added

- **Custom miner order, shared with the ESP32 panel.** The dashboard's
  drag-and-drop arrangement is now persisted server-side (settings key
  `_miner_order`, new `GET/POST/DELETE /api/miners/order` endpoints)
  instead of per-browser localStorage, and `panel_feed()` applies the
  same order to the `minerwatch/panel` MQTT blob — so the ESPHome touch
  panel shows cards exactly as arranged on the dashboard, with no
  firmware change (the payload is only permuted, fields and structure
  untouched). Entries are keyed by the stable sanitized-MAC id, so a
  miner that is deleted and re-added reclaims its slot; new miners
  append at the end, removed ones are skipped without shifting the
  rest unexpectedly, and with no saved order the feed stays
  byte-identical to before. Existing per-browser arrangements migrate
  to the server automatically on first load.

### Changed

- **Avalon "VR" readings are labelled for what they are.** Avalon
  miners (canaan family) have no VR sensor: the driver deliberately
  feeds the air outlet temperature (`OTemp`) into the VR slot as a
  thermal proxy. The miner page already said "Air outlet temp" — now
  the dashboard tile ("Air out", with an explanatory hover tooltip),
  the temperature chart tooltip and the Home Assistant discovery name
  agree. Labels only: the MQTT field (`temp_vr_c` / panel `vr`), the
  HA entity (`unique_id`, topic, template) and the ESP32 panel payload
  are all unchanged.

### Fixed

- **Alert banner unreadable on phones.** With a single unread alert the
  banner offered no expand control at all (it only appeared with two or
  more), so a long message stayed truncated with no way to read it; and
  even with several alerts the only toggle was the small chevron
  button. The whole summary line is now a tap target that expands the
  list (where messages wrap in full), and the expand button is always
  present — "Show" with one alert, "Show all (n)" with more.

## [1.12.0] — 2026-06-11

### Added

- **BitForge Nano support (forge-os).** WantClue's dual-BM1370 board joins
  as the new `bitforge` family. forge-os is AxeOS-derived, so monitoring,
  fan/frequency/voltage control, dual pools, donate-hashrate, live shares
  and the Guardian all work. The driver maps the forge-os dialect onto the
  standard readouts: per-ASIC `chiptemp1`/`chiptemp2` (hottest governs,
  the firmware's averaged `temp` feeds the average row), INA260 board
  current as PSU amps, and the fan duty under every firmware spelling
  (`fanspeed` on the v1.0 factory firmware, `fanSpeed`/`manualFanSpeed`
  on v1.5+). Auto-discovery keys on `deviceModel` where `/api/system/asic`
  exists and falls back to the forge-os-only `chiptemp1` telemetry on
  v1.0, which lacks the endpoint; 2× BM1370 names the board "BitForge
  Nano". The Guardian gets a board-appropriate VR band (77–80 °C: the
  Nano's TPS546 sits ~71 °C at stock and its firmware only throttles the
  VR at 105 °C, so the Bitaxe-tuned 67–70 °C band would have pinned it
  below stock), and the hashrate validity check uses the smallCoreCount ×
  asicCount fallback since forge-os reports no `expectedHashrate`.
  Verified against a real Nano running the v1.0 factory firmware.

### Fixed

- **Voltage co-tuner hard-tripped on 12 V boards.** The Vin sag/overshoot
  guard compared every input rail against the 5 V window (4800–5500 mV),
  so 12 V boards (NerdQAxe, BitForge Nano) reading ~12 000 mV hit the
  cutoff on every tick and were pushed to the frequency floor. The window
  now scales to the rail (11 520–13 200 mV on 12 V) with the same relative
  margins.
- **Umbrel widgets never rendered.** umbreld parses the `refresh` interval
  from the widget data response (the manifest value is never merged in),
  so payloads without it made `widget.data` throw and both widgets sat on
  their loading skeleton forever. The endpoints now include `refresh` in
  every payload.
- **Docker healthcheck failed whenever authentication was enabled.** The
  probe calls `/api/health` with no session, got 401 and permanently
  flagged the container unhealthy. The endpoint is now auth-exempt; it
  exposes the same status + version the public `/api/version` already does.

## [1.11.0] — 2026-06-11

### Added

- **Umbrel desktop widgets.** Two widgets for the umbrelOS home: fleet stats
  (hashrate, miners online, best share, max chip temp) with a celebration
  layout for 7 days after a block find, and a per-miner list (hashrate +
  temp, offline miners first, "+N more" aggregate row beyond five). The
  endpoints are served through Umbrel's app_proxy (the `web` service is
  host-networked and has no bridge IP for umbreld to resolve) and stay
  auth-exempt — they expose only coarse fleet numbers.
- **Tidy up block trophies.** Every trophy row in the dashboard card has an
  X to hide it — strictly one at a time, there is no hide-all. Hidden
  trophies stay in the DB and keep feeding the Umbrel widget, the stats and
  the poller's anti-duplication guard (a deletion would let the same share
  re-fire); they can be restored from Settings → General.
- **What's new after updates.** A small dialog appears once per version with
  the highlights of the release (the bold leads of this very changelog,
  extracted server-side via `/api/whatsnew`) plus star/donate buttons and a
  link to the full changelog. A fresh install seeds silently; closing it in
  any way marks the version as seen.
- **New ways to support the project.** Dismissible star/donation prompts on
  new all-time best shares and block finds: star first, donation once the
  star is settled, and a block find asks immediately — while donors are
  never asked again (an active donation silences everything). Telegram
  milestone messages carry a short star/donations footer, rate-limited and
  removable via `alerts.telegram_star_footer`; the README gained a proper
  Donations section including hashrate lending.

## [1.10.11] — 2026-06-10

### Added

- **Watched Bitcoin addresses — get notified when a payment lands.** New card in
  Settings → Alerts: list one or more BTC addresses (optional label, "+" to add
  more) and MinerWatch notifies on every **new confirmed incoming transaction**,
  on the same channels as every other alert (browser push + Telegram). Clicking
  the push notification opens the address page on mempool.space; the Telegram
  message carries both the transaction and the address links. Incoming amounts
  at or below a configurable dust threshold (default 546 sats) are still
  notified but flagged as "Potential dust attack — do not spend it".
  Detection details: one mempool.space query per address per minute (the same
  source already used for the network difficulty), confirmed-only so an RBF
  replacement can never produce a ghost notification, incoming-only so your own
  spends and change stay silent, a silent bootstrap so adding an address with
  existing history doesn't flood you, and persistent per-txid dedup so restarts
  never replay old transactions.

## [1.10.9] — 2026-06-08

### Changed

- **Guardian: instability is now judged against the theoretical hashrate.**
  Replaces the 1.10.5 "dropped below the best we've seen" heuristic with a
  physics-based test: the theoretical hashrate for a frequency is
  `freq × cores / 1e6` TH/s (cores = AxeOS `smallCoreCount` × `asicCount`), and a
  point is valid only when the measured hashrate is at least 97% of it (a 3%
  tolerance, tightened from the bitaxe benchmark's 6% so a *continuous* governor
  stays nearer the efficient edge instead of pushing frequency into rising errors). The governor raises frequency **only
  while the current point is valid**, and steps down — pinning a soft ceiling
  just below — when it isn't. So it judges a frequency immediately and in
  absolute terms instead of needing to first observe a "good" run, which fixes
  the over-overclock chase more cleanly. This is the safe, frequency-only
  foundation (Phase 1) of the continuous voltage+frequency co-tuner specified in
  `docs/guardian-cotuner-design.md`. The post-change settle window is raised to
  180 s to match field tuners (AxeOS's hashrate EWMA lags a minute or two).
- **Guardian: temperature deadband narrowed from 5 °C to 3 °C.** It now settles
  closer to your temperature limit (a little more hashrate) while staying wide
  enough not to hunt at the edge.
- **Guardian panel tidy-up + higher voltage ceiling.** Removed the per-miner
  Frequency-floor field (the global floor still applies as the runaway-down
  safety net — now 485 MHz, just below the Gamma's 525 stock — it just isn't an
  editable per-miner knob anymore); relabelled the
  max-temperature hint from "recovers at" to "Hold setting at"; and raised the
  voltage co-tuner's default ceiling from 1300 mV to 1350 mV (still under the
  benchmark's 1400 mV cap — the power and temperature cutoffs remain the real
  bound).
- **Guardian: ASIC error-% brake.** A new `error_pct_max` (default 2%) treats the
  chip as unstable when the firmware's `errorPercentage` climbs past it, even if
  the hashrate still meets `valid_pct`. In voltage mode the co-tuner raises
  voltage to cure it; frequency-only mode steps down. This catches the regime
  where pushing frequency at fixed voltage drives errors up before effective
  hashrate visibly drops.

### Added

- **Guardian live readout shows the theoretical (expected) hashrate** alongside
  the effective one, plus a validity flag, so you can see at a glance whether the
  chip is keeping up at the current frequency. It also reads the theoretical
  value straight from the firmware's `expectedHashrate` (exact match with the
  AxeOS dashboard) and surfaces the firmware error % and core voltage.
- **Guardian: continuous voltage + frequency co-tuner (Phase 2, opt-in).** When
  enabled per-miner — behind a confirmation, off by default — the Guardian also
  tunes core voltage: it raises voltage to cure instability and hold higher
  frequencies, and lowers it alongside frequency to shed heat efficiently, to
  keep each miner at the best operating point under your temperature limit.
  Bounded by hard cutoffs checked every tick (chip/VR temperature, power — taken
  from the firmware's own `maxPower` — and input-voltage band), a conservative
  voltage envelope, and the 75°C watchdog underneath; it backs both levers off
  the instant a cutoff trips. Gated by a global master switch
  (`guardian.v2_voltage_enabled`) plus the per-miner opt-in. See
  `docs/guardian-cotuner-design.md`.

### Fixed

- **Co-tuner steered on the measured core voltage instead of the set one.**
  AxeOS's `coreVoltageActual` droops under load, so as frequency rose the voltage
  appeared to "drop back," and the cure step could compute its +10 mV from the
  drooped value and actually *lower* the set point. The co-tuner now steers on
  the set `coreVoltage`, so voltage steps are clean and monotonic and the live
  readout shows the set value (stable, not the load-dependent measurement).
- **"Disable the Guardian to reset the soft ceiling" didn't take effect
  promptly.** The in-memory soft ceiling only cleared on the next governor tick,
  so a quick disable→re-enable (within one interval) never cleared it, and a page
  reload didn't help (it's backend state). Any change to a miner's Guardian
  settings now resets its in-memory state immediately — the soft ceiling and the
  live readout clear right away.

## [1.10.6] — 2026-06-06

### Added

- **Ambient temperature on the dashboard.** The home page now shows the ambient
  temperature relayed from the optional MQTT sensor — the same current/min/max
  reading, texts and colour scale (blue→red gradient for the current value, light
  blue Min, red Max) as the ESP32 panel's bottom row, in a full-width card near
  the top of the dashboard. It appears only when the relay has data, so installs
  without an ambient sensor are unaffected. Backed by a new
  `GET /api/fleet/ambient_temp` endpoint.

## [1.10.5] — 2026-06-06

### Fixed

- **Guardian no longer over-overclocks into ASIC instability.** The recovery
  branch only watched the VR/chip temperature and the *pool* reject rate, but
  ASIC hardware errors (invalid nonces from pushing frequency past what the core
  voltage supports) crater *effective* hashrate without ever reaching the pool —
  so the reject % stayed low and blind to them. Worse, the failing chip did less
  real work, drew less power and ran cooler, which the governor read as "more
  headroom" and kept raising frequency: a runaway where real hashrate collapsed
  while the Guardian climbed. It now also watches effective hashrate against the
  best each chip has proven it can sustain at a given frequency; a drop past
  `hashrate_drop_pct` (default 15%) is treated as instability — it steps
  frequency down and pins an in-memory soft ceiling just below the breaking
  point so recovery settles under it instead of hunting back in. Disable and
  re-enable a miner's Guardian to clear the soft ceiling.

### Added

- **Guardian live readout now shows effective hashrate and the ASIC
  hardware-error count** (with the per-interval delta), and flags when a miner's
  ceiling has been capped after a hashrate regression.

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
  on every push and weekly as a non-blocking signal, flagging known CVEs in the
  Python and frontend dependencies before they bite.

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
