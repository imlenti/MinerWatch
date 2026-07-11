// TypeScript shapes mirroring the FastAPI responses MinerWatch returns.
// Kept in one place because most components touch at least one of them.
// When the backend grows a field, this is the only file that has to
// change to make the new field visible to the entire frontend.
//
// Convention: we annotate optional/nullable fields with `| null` because
// the Python backend returns `null` literally (it doesn't omit keys).

export type MinerFamily = 'bitaxe' | 'nerdoctaxe' | 'bitforge' | 'nmaxe' | 'canaan' | 'braiins' | 'luxos';

export interface MinerRecord {
  id: number;
  family: MinerFamily;
  host: string;
  port: number | null;
  name: string;
  mac: string | null;
  model: string | null;
  notes: string | null;
  enabled: number;
  fan_mode: 'firmware' | 'manual' | 'minerwatch' | null;
  auto_target_c: number | null;
  fan_min_override: number | null;
  fan_max_override: number | null;
  // Per-miner overheat-watchdog trigger °C (Avalon/Canaan only). null → the
  // global 75°C default. The fan-to-100% release trails it by a fixed 10°C.
  watchdog_overheat_c: number | null;
  // Guardian (runtime frequency governor) per-miner settings.
  guardian_enabled: number | null;          // 0 | 1 (SQLite int)
  guardian_max_freq_mhz: number | null;      // ceiling ("max frequency")
  guardian_freq_floor_mhz: number | null;    // optional floor override
  guardian_temp_source: string | null;       // 'vr' (default) | 'chip'
  guardian_max_temp_c: number | null;         // per-miner max temp (high threshold)
  last_status: string | null;
  // Offline-alert mute (0 | 1). When 1, disconnect alerts are silenced for
  // this miner until it reconnects — see the dashboard banner Mute button.
  offline_muted: number;
  // Ambient sensor (room) this miner sits in: a 12-hex device sensor_id, or
  // null when unassigned. Drives the room-temperature overlay on the History
  // "Temperature" chart — no assignment means no ambient line.
  ambient_sensor_id: string | null;
  // Cached display name of the assigned sensor, so the room label stays
  // friendly even while that sensor is offline. Null when unassigned.
  ambient_sensor_name: string | null;
}

export interface MetricSample {
  ts: number;
  hashrate_ths: number | null;
  power_w: number | null;
  temp_chip_c: number | null;
  temp_vr_c: number | null;
  fan_rpm: number | null;
  fan_pct: number | null;
  frequency_mhz: number | null;
  voltage_mv: number | null;
  uptime_s: number | null;
  accepted: number | null;
  rejected: number | null;
  best_difficulty: number | null;
  pool_url: string | null;
  worker: string | null;
}

// Health status of a single ASIC chip, as reported by the LuxOS
// ``healthchipget`` command. "Y" = healthy, "N" = unhealthy/dead,
// "Unknown" = the firmware hasn't classified this chip yet (e.g. it
// was just powered on or the health check is currently in progress).
export type ChipHealth = 'Y' | 'N' | 'Unknown';

export interface ChipHealthRecord {
  chip: number | null;
  row: number | null;
  column: number | null;
  domain: number | null;
  healthy: ChipHealth;
  is_checking: boolean | null;
  // Optional fields — LuxOS omits these when health == "Unknown".
  frequency: number | null;
  ghs_1m: number | null;
  ghs_5m: number | null;
  ghs_15m: number | null;
  score: number | null;
  // Per-chip temperature is only reported by S21/T21-class firmware.
  chip_temp_c: number | null;
  hash_count: number | null;
  hash_expected: number | null;
}

// Per-hashboard snapshot. ``temps_extra`` is keyed by the LuxOS
// position name (BottomLeft / BottomRight / TopLeft / TopRight) and
// ``temps_labels`` maps the same key to a human-readable label
// ("Board Exhaust", "Board Intake", …) that comes from the METADATA
// section of the ``temps`` reply. Both are empty objects on builds
// that don't expose this metadata; the frontend then falls back to
// rendering the raw position name.
export interface BoardSnapshot {
  id: number;
  status: string | null;
  enabled: boolean | null;
  connector: string | null;
  frequency_mhz: number | null;
  voltage_v: number | null;
  hashrate_ths: number | null;
  hashrate_5s_ths: number | null;
  nominal_ths: number | null;
  // Per-board hardware error rate (%), 2 decimals. Computed by the
  // backend from Hardware Errors / Diff1 Work (LuxOS reports the native
  // Device Hardware% as a constant 0). Null when uncomputable.
  hw_error_rate: number | null;
  temp_chip_c: number | null;
  temps_extra: Record<string, number>;
  temps_labels: Record<string, string>;
  chips_total: number | null;
  chips_healthy: number | null;
  chips_unhealthy: number | null;
  chips_unknown: number | null;
  chips: ChipHealthRecord[];
}

// One pool slot configured on a miner — populated by every driver
// since v0.x; see backend/miners/base.py:PoolSnapshot.
export interface LivePool {
  url: string | null;
  user: string | null;
  status: string | null;
  priority: number | null;
  accepted: number | null;
  rejected: number | null;
  stale: number | null;
  last_share_ts: number | null;
  active: boolean | null;
  slot: 'primary' | 'fallback' | string | null;
  // Round-trip latency to the pool (ms), measured by the miner itself.
  // Exposed by Bitaxe (responseTime), NerdQAxe (per-pool pingRtt) and
  // Avalon (PING in MM ID0). Null for Braiins/LuxOS (cgminer pools has
  // no latency field).
  ping_ms: number | null;
  // Ping packet-loss % — NerdQAxe only; null elsewhere.
  ping_loss: number | null;
}

// One physical fan. Today only LuxOS populates this; for other
// families the array stays empty and the frontend falls back to the
// legacy single-fan / fan_2 rendering.
export interface FanSnapshot {
  id: number;
  rpm: number | null;
  speed_pct: number | null;
  connector: string | null;  // e.g. "J12 | J14" — LuxOS only
}

// Live sample shape mirrors backend/miners/base.py:MinerSample as serialised
// by dataclasses.asdict. Most fields overlap with MetricSample; extras
// like `raw` and the air-inlet/outlet temps live only on the live blob.
export interface LiveSample {
  family: MinerFamily;
  host: string;
  online: boolean;
  error: string | null;
  // Firmware paused/standby (AxeOS `miningPaused`): hashing stopped, ASIC
  // powered down, controller still online. Null when the firmware doesn't
  // report it (older builds / families without the feature).
  mining_paused: boolean | null;
  mac: string | null;
  model: string | null;
  // ASIC chip model (e.g. "BM1370"), derived from the model name by the
  // backend. Null when the model is unknown/unmapped.
  chip_model: string | null;
  hostname: string | null;
  firmware_version: string | null;
  hashrate_ths: number | null;
  power_w: number | null;
  efficiency_w_per_ths: number | null;
  temp_chip_c: number | null;
  // Second chip sensor on multi-ASIC AxeOS boards (Bitaxe SupraHex):
  // the firmware reports `temp`/`temp2` and the backend threads the
  // second one through here. Null on single-sensor devices.
  temp_chip_2_c: number | null;
  temp_vr_c: number | null;
  temp_outlet_c: number | null;
  temp_inlet_c: number | null;
  temp_avg_c: number | null;
  fan_rpm: number | null;
  fan_pct: number | null;
  fans_extra: Record<string, number>;
  // Structured per-fan list (LuxOS only at the moment). When present
  // the frontend renders one tile per fan with RPM/% and the connector
  // label; otherwise it falls back to the legacy single-fan rendering.
  fans: FanSnapshot[];
  // NerdOctaxe-only: the firmware exposes a second physical fan.
  // Stay null on Bitaxe and on the cgminer families.
  fan_rpm_2: number | null;
  fan_pct_2: number | null;
  frequency_mhz: number | null;
  voltage_mv: number | null;
  // Active firmware work mode (Avalon: 0=Low, 1=Mid, 2=High). Null on
  // families without the concept. Drives the WorkModeControls highlight.
  workmode: number | null;
  asic_count: number | null;
  // Multi-hashboard miners report one entry per physical board plus
  // the totals. ``board_count`` and ``chip_count`` separate the two
  // concepts that ``asic_count`` historically conflated.
  board_count: number | null;
  chip_count: number | null;
  boards: BoardSnapshot[];
  // PSU draw in Amps. Populated by the NerdOctaxe driver from the
  // firmware's `currentA` field; null elsewhere.
  current_a: number | null;
  // Aggregate "hardware error" counter — count of nonces the ASIC
  // returned that failed validation. NerdOctaxe firmware emits this
  // as `duplicateHWNonces`. Bitaxe doesn't surface it, so null there.
  hw_errors: number | null;
  // Fleet-wide hardware error rate (%), aggregated across boards.
  // Computed by the LuxOS driver (its native Device Hardware% is always
  // 0). 2-decimal presentation handled in the UI. Null elsewhere.
  hw_error_rate: number | null;
  uptime_s: number | null;
  accepted: number | null;
  rejected: number | null;
  best_difficulty: number | null;
  best_difficulty_alltime: number | null;
  network_difficulty: number | null;
  pool_url: string | null;
  worker: string | null;
  // Dual-pool fields (NerdOctaxe firmware). `pool_active` is
  // "primary" | "fallback" when the firmware tells us which one is
  // currently mining, or null otherwise.
  pool_url_fallback: string | null;
  worker_fallback: string | null;
  pool_active: 'primary' | 'fallback' | string | null;
  // Structured per-pool list — one entry per pool slot configured on
  // the miner, including fallback(s). All drivers now populate this.
  pools: LivePool[];
  raw: Record<string, unknown> | null;
}

export interface MinerListEntry extends MinerRecord {
  last_metric: MetricSample | null;
  live_online: boolean | null;
  live_error: string | null;
  // Firmware standby (AxeOS pause / NerdQAxe shutdown): the miner is online
  // but deliberately stopped. Lets the card show "standby" instead of
  // "online". Null when not reported / not yet polled.
  live_mining_paused: boolean | null;
}

export interface MinerListResponse {
  miners: MinerListEntry[];
}

// Persisted fleet display order — sanitized-MAC ids (lowercase MAC
// without separators, or `mw<db_id>` when no MAC is known). The same
// list drives the dashboard grid and the ESP32 panel feed.
export interface MinerOrderResponse {
  order: string[];
}

// Persisted order of the main dashboard's movable sections (stable
// section ids). Frontend-only display preference; see `useDashboardLayout`.
export interface DashboardLayoutResponse {
  order: string[];
}

// One row from /api/pools — a single (miner, pool slot) pair.
//
// Field availability varies by driver (see backend/miners/base.py:
// :class:`PoolSnapshot`). In short:
//   * cgminer-family (Braiins/LuxOS/Canaan): every field can be
//     populated; ``status`` is an explicit Alive/Dead/Disabled from
//     the firmware.
//   * Bitaxe: ``stale`` and ``last_share_ts`` are always null because
//     AxeOS doesn't surface them; ``status`` is null (we don't fake
//     Alive/Dead per-pool — the miner's overall ``live_online`` flag
//     is the right signal there).
//   * NerdOctaxe: same as Bitaxe but two rows when a fallback pool is
//     configured; the firmware reports ``accepted`` / ``rejected``
//     globally rather than per-slot, so they only appear on the
//     ``active`` slot and are null on the inactive one.
export interface PoolRow {
  miner_id: number;
  miner_name: string;
  miner_host: string;
  family: MinerFamily;
  live_online: boolean | null;
  live_error: string | null;
  url: string | null;
  user: string | null;
  // "alive" | "dead" | "disabled" | null — null means "unknown",
  // typical for AxeOS where the firmware has no per-pool flag.
  status: string | null;
  priority: number | null;
  accepted: number | null;
  rejected: number | null;
  stale: number | null;
  last_share_ts: number | null;
  active: boolean | null;
  // "primary" | "fallback" | null — only filled for the AxeOS family;
  // cgminer firmwares use ``priority`` instead.
  slot: 'primary' | 'fallback' | string | null;
  // Pool ping (ms), measured by the miner. Null where the firmware
  // doesn't expose it (Braiins, LuxOS).
  ping_ms: number | null;
  // Ping packet-loss % — NerdQAxe only; null elsewhere.
  ping_loss: number | null;
}

export interface PoolsResponse {
  pools: PoolRow[];
}

export interface Capabilities {
  set_fan: boolean;
  set_frequency: boolean;
  set_voltage: boolean;
  // Firmware performance presets (Avalon work mode: Low/Mid/High).
  set_workmode: boolean;
  restart: boolean;
  // AxeOS Standby — POST /api/system/pause + /resume (soft, no reboot).
  // False on families whose firmware lacks it (forge-os, cgminer).
  pause: boolean;
  // NerdQAxe Standby — POST /api/system/shutdown (powers down the ASIC).
  // No soft resume: resume is via restart. False on other families.
  shutdown: boolean;
  set_pool: boolean;
}

// ---------- Donate hashrate ----------

export interface DonationInfo {
  btc_address: string;
  worker: string;
  worker_name: string;
  pool_url: string;
  pool_port: number;
  min_hours: number;
  max_hours: number;
  default_hours: number;
}

// One in-flight (miner, donation) row for the active-donations table.
export interface DonationMinerRow {
  id: number;            // donation_miners.id (used by the per-row STOP)
  donation_id: number;
  miner_id: number;
  miner_name: string | null;
  family: MinerFamily | null;
  host: string | null;
  status: 'active' | 'unreachable' | 'reverted' | 'error' | string;
  ends_ts: number;
  seconds_remaining: number;
  online: boolean;
  hashrate_ths: number | null;
  pool_url: string | null;
  confirmed: boolean;    // poller sees it mining the donation pool
  last_error: string | null;
}

export interface DonationListResponse {
  donations: DonationMinerRow[];
  count: number;
}

// Per-miner outcome returned by POST /api/donations.
export interface StartDonationMinerResult {
  miner_id: number;
  status: 'active' | 'error' | 'unsupported' | string;
  error?: string;
}

export interface StartDonationResponse {
  donation_id: number | null;
  ends_ts: number;
  miners: StartDonationMinerResult[];
}

export interface MinerDetailResponse {
  miner: MinerRecord;
  last_metric: MetricSample | null;
  live_sample: LiveSample | null;
  capabilities: Capabilities;
}

export interface BestRecord {
  miner_id: number;
  miner_name: string;
  value: number;
  ts: number;
}

export interface BestRecordsResponse {
  session: BestRecord | null;
  alltime: BestRecord | null;
}

export interface BestRecordRanked {
  miner_id: number;
  miner_name: string;
  family: MinerFamily;
  value: number;
  ts: number;
}

export interface BestRecordsTopResponse {
  scope: 'session' | 'alltime';
  limit: number;
  entries: BestRecordRanked[];
}

export interface PredictionWindow {
  expected_time_s: number | null;
  probability: {
    '1h': number;
    '24h': number;
    '7d': number;
  };
}

// Which coin the "Find a block (solo)" odds are computed against.
// 'auto' = the coin the fleet is actually mining (network difficulty from
// stratum); 'btc'/'bch' = that coin's live network difficulty from a public
// explorer, so the user can compare odds across coins at the same hashrate.
export type PredictionCoin = 'auto' | 'btc' | 'bch';

export interface PredictionResponse {
  fleet_hashrate_ths: number | null;
  best_alltime: BestRecord | null;
  network_difficulty: number | null;
  // Echo of the coin the backend used for find_block. Optional for
  // backward-compat with older payloads that didn't include it.
  coin?: PredictionCoin;
  predictions: {
    beat_best: PredictionWindow | null;
    find_block: PredictionWindow | null;
  };
}

export interface BlockFind {
  id: number;
  miner_id: number;
  miner_name: string;
  ts: number;
  share_difficulty: number;
  network_difficulty: number;
  block_height: number | null;
  // Dashboard-only visibility: 1 = dismissed via the per-trophy X.
  // Hidden trophies still exist everywhere else (DB, Umbrel widget,
  // stats) and can be restored from Settings.
  hidden: 0 | 1;
}

export interface BlockFindsResponse {
  block_finds: BlockFind[];
}

// One ambient sensor pushing readings to MinerWatch over HTTP.
// `current_c` is a 60s moving average and is null when the reading is
// stale (shown as "-"); `min_c` / `max_c` are session extremes.
// `name` is the user-set label and `sensor_id` the device identity.
export interface AmbientSensor {
  sensor_id: string;
  name: string;
  current_c: number | null;
  min_c: number | null;
  max_c: number | null;
  available: boolean;
  has_data: boolean;
}

// GET /api/fleet/ambient_temp returns one entry per live sensor. An empty
// list means nothing has arrived yet (or every sensor went silent), and the
// dashboard hides the card in that case.
export interface AmbientFleet {
  sensors: AmbientSensor[];
}

// The backend returns one row per bucket as { bucket_ts, total_ths }.
// We keep the names backend-exact so a stray rename here is loud
// rather than silently producing an empty chart.
export interface FleetHashratePoint {
  bucket_ts: number;
  total_ths: number;
}

export interface FleetHashrateResponse {
  from_ts: number;
  to_ts: number;
  bucket_seconds: number;
  /** Storage tier the backend resolved for this range (``metrics`` |
   *  ``metrics_1m`` | ``metrics_1h``). Older backends may omit it. */
  tier?: string;
  points: FleetHashratePoint[];
}

export interface MetricsRangeResponse {
  miner_id: number;
  from_ts: number;
  to_ts: number;
  tier: 'raw' | '1m' | '1h';
  metrics: MetricSample[];
}

// One stored ambient (room) temperature sample, pushed by an external
// sensor (HTTP). Fleet-wide, so there is no miner_id — see the History tab
// "Temperature" overlay. `temp_c` is the bucket average on the rollup tiers.
export interface AmbientHistoryPoint {
  ts: number;
  temp_c: number | null;
}

export interface AmbientHistoryResponse {
  from_ts: number;
  to_ts: number;
  // Storage tier the backend resolved (``metrics`` | ``metrics_1m`` |
  // ``metrics_1h``); mirrors the per-miner metrics contract.
  tier: string;
  points: AmbientHistoryPoint[];
}

export interface AuthStatus {
  enabled: boolean;
  authenticated?: boolean;
  // Added in 1.10.x — all optional so an older backend (which only
  // returns `enabled`) still type-checks and the UI degrades gracefully.
  password_set?: boolean;
  bind_is_loopback?: boolean;
  needs_setup?: boolean;
  // True once the operator has explicitly opted out of the auto-scan
  // security warning. Lets the dashboard stop intercepting scans.
  scan_ack?: boolean;
}

export interface HealthResponse {
  status: 'ok';
  version: string;
}

// Subset of /api/settings we actually read from the frontend. The
// endpoint returns more fields (auth subset, telegram_token_set, …)
// but the dashboard only needs polling cadence + temperature limits
// to render the toolbar subtitle and the critical-temperature banner.
export interface SettingsCurrent {
  polling: {
    interval_seconds: number;
    request_timeout: number;
    hashrate_smoothing_seconds: number;
  };
  alerts: {
    temp_chip_threshold: number;
    temp_vr_threshold: number;
    offline_threshold_seconds: number;
    repeat_seconds: number;
    notifications_enabled: boolean;
    push_enabled: boolean;
    telegram_enabled: boolean;
    telegram_chat_id?: string | null;
    telegram_token_set?: boolean;
    wallet_watch_enabled?: boolean;
    // JSON string: [{"address": "bc1…", "label": "Donations"}, …]
    wallet_watch_addresses?: string;
    wallet_watch_dust_sats?: number;
  };
  storage: {
    retention_raw_hours: number;
    retention_1m_days: number;
    retention_1h_days: number;
  };
  network: {
    scan_cidr: string;
    scan_timeout: number;
  };
  auth_enabled: boolean;
  guardian: {
    enabled: boolean;
    interval_seconds: number;
    hashrate_average_window_seconds: number;
  };
}

export interface SettingsResponse {
  current: SettingsCurrent;
  stored: Record<string, string>;
}

export type AlertSeverity = 'info' | 'warning' | 'critical';
export type AlertCode = 'temp_chip' | 'temp_vr' | 'offline' | 'recovered' | string;

export interface AlertEntry {
  id: number;
  miner_id: number | null;
  ts: number;
  severity: AlertSeverity;
  code: AlertCode;
  message: string;
  acknowledged: number; // 0 | 1 (SQLite int)
}

export interface AlertsResponse {
  alerts: AlertEntry[];
}

// Manual "Add miner" payload. Only the address is sent: the backend
// connects to the miner and auto-detects family / port / MAC / model /
// name with the same fingerprint auto-discovery uses. Notes are the one
// optional, user-supplied field.
export interface MinerCreatePayload {
  host: string;
  notes?: string | null;
}

export interface DiscoveryFound {
  family: MinerFamily;
  host: string;
  port: number;
  mac: string | null;
  name: string;
  added: boolean;
  reason?: string;
}

export interface DiscoveryResponse {
  registered: number;
  miners: DiscoveryFound[];
}

export interface TelegramChat {
  chat_id: string;
  label: string;
  type: string;
}

export interface TelegramDiscoverResponse {
  chats: TelegramChat[];
}

export interface PushTestResponse {
  subscribers: number;
}

// ---------- Guardian (runtime frequency governor) ----------

// Live readout for one miner, published by the Guardian loop each tick.
// Null when the governor hasn't evaluated this miner yet (e.g. just enabled,
// or no poll sample available).
export interface GuardianLive {
  miner_id: number;
  frequency_mhz: number | null;
  ceiling_mhz: number | null;
  floor_mhz: number | null;
  // Governed sensor reading + which sensor it is. ``vr_temp_c`` is kept for
  // backward compatibility (populated only in VR mode).
  temp_c: number | null;
  temp_source: 'vr' | 'chip';
  vr_temp_c: number | null;
  reject_pct: number | null;
  // Effective hashrate + ASIC hardware-error signals behind the regression
  // brake; soft_ceiling_mhz is the in-memory cap pinned after a regression.
  hashrate_ths: number | null;
  expected_ths: number | null;   // theoretical hashrate for the current freq
  valid: boolean | null;         // hashrate >= valid_pct of theoretical (null = not yet judged)
  error_pct: number | null;      // firmware errorPercentage (AxeOS dashboard "error %")
  voltage_mv: number | null;        // core voltage (Phase 2 co-tuner)
  target_voltage_mv: number | null; // voltage the co-tuner is steering toward
  asic_errors: number | null;
  asic_error_delta: number | null;
  soft_ceiling_mhz: number | null;
  reason: string;
  changed: boolean;
  ts: number;
}

// Global defaults echoed from GuardianCfg so the UI can show the active
// thresholds/steps without hard-coding them.
export interface GuardianDefaults {
  interval_seconds: number;
  vr_high_c: number;
  vr_low_c: number;
  chip_high_c: number;
  chip_low_c: number;
  watchdog_c: number;      // 75°C chip overheat watchdog (chip-mode upper bound)
  reject_pct_max: number;
  valid_pct: number;
  step_down_vr_mhz: number;
  step_down_err_mhz: number;
  step_up_mhz: number;
  frequency_floor_mhz: number;
  v_ceiling_mv: number;
  v_floor_mv: number;
  v_step_mv: number;
}

export interface GuardianStatusResponse {
  enabled: boolean;        // global feature flag
  supported: boolean;      // family + capability supports frequency control
  miner_enabled: boolean;  // per-miner opt-in
  max_freq_mhz: number | null;
  freq_floor_mhz: number | null;
  temp_source: 'vr' | 'chip';   // which sensor governs frequency
  max_temp_c: number | null;    // per-miner high threshold (null → source default)
  voltage_enabled: boolean;     // per-miner opt-in for the voltage co-tuner (Phase 2)
  supports_voltage: boolean;    // family exposes voltage control
  voltage_master: boolean;      // global master switch for the voltage lever
  current_freq_mhz: number | null;
  defaults: GuardianDefaults;
  live: GuardianLive | null;
}

// Host metrics surfaced by /api/system/info and /api/system/snapshot.
// The shapes here mirror backend/system_info.py exactly: when in doubt
// match the Python keys 1:1 rather than re-flattening, because the
// backend returns nested groups (cpu/memory/disk/fan/throttled) and
// every divergence is a silent rendering bug.

export interface SystemInfo {
  is_raspberry: boolean;
  // Whether the System page is worth showing on this host (Linux + a
  // real hardware signal: CPU temp sensor, vcgencmd, or a fan). Drives
  // both the sidebar entry and the page; see useDashboardLayout's sibling
  // reasoning in system_info.py.
  supported: boolean;
  model: string | null;
  kernel: string | null;
  ram_total_bytes: number | null;
  cpu_count: number | null;
  has_vcgencmd: boolean;
  fan: {
    controllable: boolean;
    max_state: number | null;
    has_rpm: boolean;
    cooling_path: string | null;
    rpm_path: string | null;
  };
}

export interface SystemCpu {
  percent: number | null;
  per_core: number[] | null;
  freq_mhz: number | null;
  freq_max_mhz: number | null;
}

export interface SystemMemory {
  used_bytes: number | null;
  total_bytes: number | null;
  percent: number | null;
}

export interface SystemDisk {
  used_bytes: number | null;
  total_bytes: number | null;
  free_bytes: number | null;
  percent: number | null;
}

export interface SystemThrottled {
  raw: string | null;
  now_undervoltage: boolean | null;
  now_freq_capped: boolean | null;
  now_throttled: boolean | null;
  now_soft_temp_limit: boolean | null;
  ever_undervoltage: boolean | null;
  ever_freq_capped: boolean | null;
  ever_throttled: boolean | null;
  ever_soft_temp_limit: boolean | null;
}

export interface SystemFanSnapshot {
  controllable: boolean;
  rpm: number | null;
  state: number | null;
  max_state: number | null;
  percent: number | null;
}

export interface SystemSnapshot {
  ts: number;
  uptime_seconds: number | null;
  load_average: [number, number, number] | null;
  cpu: SystemCpu;
  memory: SystemMemory;
  swap: SystemMemory;
  disk: SystemDisk;
  temperature_c: number | null;
  voltage_core: number | null;
  throttled: SystemThrottled;
  fan: SystemFanSnapshot;
  db_size_bytes: number | null;
}

// ----- Self-update (/api/version, /api/update/check, /api/update/install)

export interface VersionResponse {
  version: string;
  system: {
    os: string; // Darwin | Linux | Windows
    os_release: string;
    machine: string;
    python: string;
  };
  // True when running inside a container (Docker/Umbrel). The Update page
  // uses this to replace the in-app "Install" button with `docker compose
  // pull` instructions while still showing whether a newer release exists.
  // Optional for backward-compat with older backends that omit it.
  container?: boolean;
}

export interface UpdateCheckResponse {
  current: string;
  latest: string | null;
  available: boolean;
  release_notes_url: string | null;
  release_name: string | null;
  published_at: string | null;
  asset_url: string | null;
  asset_name: string | null;
  asset_size: number | null;
  sha256: string | null;
  requires_service_reinstall: boolean;
  error: string | null;
  checked_at: number;
}

export interface UpdateInstallResponse {
  status: 'restarting';
  previous_version: string;
  new_version: string;
  requires_service_reinstall: boolean;
}

// ----- Live per-share streaming (AxeOS only)
//
// Fed by the firmware log WebSocket via backend/log_streamer.py. Each
// event is one ASIC result: `diff` is the share difficulty, `target`
// the pool/stratum target in force, `submitted` = diff >= target (i.e.
// it was sent to the pool). `accepted` is filled by a later verdict
// event (null while pending, rare false on a reject).
//
// NOTE: `ts` arrives from the backend in epoch *seconds* (float); the
// useLiveShares hook converts it to milliseconds for charting.
export interface LiveShareEvent {
  seq: number;
  ts: number;
  diff: number;
  target: number;
  submitted: boolean;
  accepted: boolean | null;
  // True for synthetic events from firmware that logs no per-share
  // lines (forge-os v1.5+): `diff` is the pool target, i.e. a floor
  // for the real difficulty. May flip back to false via an `amend`
  // event when the backend learns the exact value.
  estimated?: boolean;
}

export interface LiveSharesStats {
  miner_id: number;
  connected: boolean;
  current_target: number | null;
  results_total: number;
  submitted_total: number;
  accepted_total: number;
  rejected_total: number;
  // True when the stream is running on verdict-derived synthetic
  // events (no per-share log lines on this firmware).
  synthetic?: boolean;
  estimated_total?: number;
  last_event_ts: number | null;
  buffered: number;
  since: number;
}

export interface LiveSharesRecentResponse {
  miner_id: number;
  supported: boolean;
  events: LiveShareEvent[];
  stats: LiveSharesStats | null;
}

// One row of the near-block Hall of Fame. `accepted` is a SQLite int
// (1/0) or null while the pool verdict is still pending.
export interface NotableShare {
  id: number;
  miner_id: number;
  ts: number;
  share_difficulty: number;
  pool_target: number | null;
  accepted: number | null;
}

export interface NotableSharesResponse {
  miner_id: number;
  supported: boolean;
  entries: NotableShare[];
}

// Once-per-version "What's new" dialog: bold changelog leads for the
// running version, extracted server-side (backend/whatsnew.py).
export interface WhatsNewHighlight {
  title: string;
  body: string;
}

export interface WhatsNewResponse {
  version: string;
  highlights: WhatsNewHighlight[];
}
