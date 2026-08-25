// Formatting helpers, ported from frontend/static/app.js. Kept in one
// file so a number rendered on the dashboard and on the miner page
// looks identical down to the last decimal.

const SI_UNITS = [
  { v: 1e24, s: 'Y' },
  { v: 1e21, s: 'Z' },
  { v: 1e18, s: 'E' },
  { v: 1e15, s: 'P' },
  { v: 1e12, s: 'T' },
  { v: 1e9, s: 'G' },
  { v: 1e6, s: 'M' },
  { v: 1e3, s: 'k' },
];

/** Compact SI-formatted difficulty (e.g. 4_290_000_000 → "4.29 G"). */
export function fmtDifficulty(value: number | null | undefined, decimals = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const n = Number(value);
  if (n === 0) return '0';
  const abs = Math.abs(n);
  for (const u of SI_UNITS) {
    if (abs >= u.v) return `${(n / u.v).toFixed(decimals)}\u00A0${u.s}`;
  }
  return decimals >= 2 ? n.toFixed(0) : n.toFixed(decimals);
}

export function fmtNum(value: number | null | undefined, decimals = 2, unit = ''): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${Number(value).toFixed(decimals)}${unit ? `\u00A0${unit}` : ''}`;
}

// Fleet hashrate formatter for the dashboard "Total hashrate" KPI. Input
// is already in TH/s. Scales to PH/s at >= 1000 TH/s, and picks decimals
// by the *displayed* magnitude (>= 100 → 1 decimal, else 2) so the figure
// keeps ~3-4 significant digits in either unit. Value and unit are
// returned separately so the KPI keeps styling the unit in its own muted
// span. Scope is deliberately the fleet total only — per-miner cards
// (always a few TH/s) keep their existing 2-decimal TH/s format.
export function fmtHashrate(ths: number | null | undefined): { value: string; unit: string } {
  if (ths === null || ths === undefined || Number.isNaN(Number(ths))) {
    return { value: '—', unit: 'TH/s' };
  }
  let v = Number(ths);
  let unit = 'TH/s';
  if (Math.abs(v) >= 1000) {
    v = v / 1000;
    unit = 'PH/s';
  }
  const decimals = Math.abs(v) >= 100 ? 1 : 2;
  return { value: v.toFixed(decimals), unit };
}

export function fmtUptime(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

export function fmtRelative(timestamp: number | null | undefined): string {
  if (!timestamp) return '—';
  const diff = Math.floor(Date.now() / 1000) - timestamp;
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

/** Smart, scale-aware ETA formatter. Used by the Predictions widget. */
export function fmtEta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)}\u00A0s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}\u00A0min`;
  if (seconds < 86400) {
    const h = seconds / 3600;
    return `${h < 10 ? h.toFixed(1) : Math.round(h)}\u00A0h`;
  }
  if (seconds < 86400 * 30) {
    const d = seconds / 86400;
    return `${d < 10 ? d.toFixed(1) : Math.round(d)}\u00A0days`;
  }
  if (seconds < 86400 * 365) {
    const m = seconds / (86400 * 30.44);
    return `${m < 10 ? m.toFixed(1) : Math.round(m)}\u00A0months`;
  }
  const y = seconds / (86400 * 365.25);
  return y < 100 ? `${y < 10 ? y.toFixed(1) : Math.round(y)}\u00A0years` : `${Math.round(y).toLocaleString()}\u00A0years`;
}

/** Probability → human label, with sane bounds at the extremes. */
export function fmtProb(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return '—';
  if (p >= 0.9995) return '> 99.9 %';
  if (p < 0.0001) return '< 0.01 %';
  const pct = p * 100;
  if (pct < 1) return `${pct.toFixed(3)} %`;
  if (pct < 10) return `${pct.toFixed(2)} %`;
  return `${pct.toFixed(1)} %`;
}

/** Threshold-based class hint for a temperature. Mirrors the vanilla CSS. */
export function tempTone(t: number | null | undefined): 'normal' | 'warm' | 'hot' | 'critical' {
  if (t === null || t === undefined) return 'normal';
  if (t >= 80) return 'critical';
  if (t >= 70) return 'hot';
  if (t >= 60) return 'warm';
  return 'normal';
}

export const FAMILY_LABEL: Record<string, string> = {
  bitaxe: 'Bitaxe',
  nerdoctaxe: 'NerdQAxe / NerdOctaxe',
  bitforge: 'BitForge',
  nmaxe: 'NMAxe',
  canaan: 'Canaan / Avalon',
  canaannano3: 'Canaan / Avalon Nano 3',
  braiins: 'Braiins / BMM',
  luxos: 'LuxOS (Antminer)',
};

/** Canaan Avalon family — Nano 3s / Q / A10 (`canaan`) and the original Nano 3. */
export function isCanaanFamily(family: string | null | undefined): boolean {
  const f = (family ?? '').toLowerCase();
  return f === 'canaan' || f === 'canaannano3';
}

// Dual-fan labelling for the NerdQAxe / NerdOctaxe family. Both boards run
// the same firmware and expose two fan channels — `fanrpm`/`fanspeed`
// (primary) and `fanrpm2`/`fanspeed2` (secondary) — but they silk-screen
// the fan connectors differently:
//   - NerdQAxe  (model contains "qaxe"): M2 (lower) = CPU fan,
//     M1 (upper) = Aux/VRM fan.
//   - NerdOctaxe (the rest of the family): C2 (lower) / C1 (upper).
// The primary channel is the lower / CPU fan, the secondary the upper /
// Aux-VRM one. Returned strings are shared by the Overview and Hardware
// tabs so both label the two fans identically.
export interface NerdFanLabels {
  primaryRole: string;       // e.g. "CPU fan (M2 lower)"
  primaryConnector: string;  // tooltip, e.g. "Connector: M2 (lower)"
  secondaryRole: string;     // e.g. "Aux/VRM fan (M1 upper)"
  secondaryConnector: string;
}

export function nerdFanLabels(model: string | null | undefined): NerdFanLabels {
  const isQaxe = (model ?? '').toLowerCase().includes('qaxe');
  const lower = isQaxe ? 'M2' : 'C2';
  const upper = isQaxe ? 'M1' : 'C1';
  return {
    primaryRole: `CPU fan (${lower} lower)`,
    primaryConnector: `Connector: ${lower} (lower)`,
    secondaryRole: `Aux/VRM fan (${upper} upper)`,
    secondaryConnector: `Connector: ${upper} (upper)`,
  };
}
