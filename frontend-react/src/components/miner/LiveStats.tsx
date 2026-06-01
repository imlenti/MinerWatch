import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { fmtDifficulty, fmtNum, fmtUptime, tempTone } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { LiveSample, MetricSample, MinerDetailResponse } from '@/lib/types';

interface Props {
  data: MinerDetailResponse;
}

/**
 * Overview tab: a single panel with all current metrics for the
 * selected miner. Prefers the live sample (in-memory, fresher) and
 * falls back to the latest DB row when polling hasn't arrived yet —
 * same logic as the vanilla `renderLiveStats`.
 */
export function LiveStats({ data }: Props) {
  const lm = data.last_metric;
  const ls = data.live_sample;
  // Prefer the live poller verdict: if the most recent poll says the
  // miner is offline, treat it as offline even when the persisted
  // last_status is momentarily stale.
  const status = ls?.online === false
    ? 'offline'
    : data.miner.last_status ?? (data.live_sample?.online ? 'online' : 'unknown');
  const error = ls?.error ?? null;

  // When offline we have no fresh readings, so blank every live metric
  // with a "—" instead of echoing the last values seen before the miner
  // went down (those are stale and read as if it were still running).
  // The Status row still reports "offline".
  const offline = status === 'offline';

  const v = <K extends keyof MetricSample & keyof LiveSample>(key: K): number | string | null | undefined => {
    if (offline) return null;
    if (ls && (ls as Record<K, unknown>)[key] !== null && (ls as Record<K, unknown>)[key] !== undefined) {
      return (ls as Record<K, unknown>)[key] as number | string | null | undefined;
    }
    return (lm as Record<K, unknown> | null | undefined)?.[key] as number | string | null | undefined;
  };

  const family = data.miner.family;
  const vrLabel = family === 'canaan' ? 'Air outlet temp' : 'Temp VR';

  const hashrate = v('hashrate_ths') as number | null;
  const power = v('power_w') as number | null;
  const tempChip = v('temp_chip_c') as number | null;
  const tempVr = v('temp_vr_c') as number | null;
  const tempIn = ls?.temp_inlet_c ?? null;
  const tempOut = ls?.temp_outlet_c ?? null;
  const tempAvg = ls?.temp_avg_c ?? null;
  const eff = power && hashrate ? power / hashrate : null;
  const uptime = v('uptime_s') as number | null;
  const accepted = v('accepted') as number | null;
  const rejected = v('rejected') as number | null;
  const fanRpm = v('fan_rpm') as number | null;
  const fanPct = v('fan_pct') as number | null;
  const freq = v('frequency_mhz') as number | null;
  const volt = v('voltage_mv') as number | null;
  const bestSession = v('best_difficulty') as number | null;

  // ---- NerdOctaxe extras ------------------------------------------
  // Only the NerdOctaxe driver populates these. They stay null for
  // every other family, in which case we skip the corresponding row
  // rather than rendering a "—" placeholder (avoids visual clutter
  // on Bitaxe/Canaan/etc. dashboards).
  const currentA = ls?.current_a ?? null;
  const fanRpm2 = ls?.fan_rpm_2 ?? null;
  const fanPct2 = ls?.fan_pct_2 ?? null;
  const hwErrors = ls?.hw_errors ?? null;
  // Rejection rate: a more "chip-error-like" metric than the raw
  // duplicate-nonce counter. (accepted + rejected) might be zero on
  // a freshly booted miner — guard the division.
  const rejectionRate =
    accepted !== null && rejected !== null && accepted + rejected > 0
      ? (rejected / (accepted + rejected)) * 100
      : null;
  // Hardware error rate (%). LuxOS computes this server-side (its native
  // Device Hardware% is hard-coded to 0); other families leave it null.
  const hwErrorRate = ls?.hw_error_rate ?? null;
  const boardCount = ls?.board_count ?? 0;
  const poolUrl = (v('pool_url') as string | null) ?? null;
  const worker = (v('worker') as string | null) ?? null;
  const poolUrlFallback = ls?.pool_url_fallback ?? null;
  const workerFallback = ls?.worker_fallback ?? null;
  const poolActive = ls?.pool_active ?? null;
  const isNerdOctaxe = family === 'nerdoctaxe';

  type Row = { label: string; value: React.ReactNode; title?: string };
  const rows: Row[] = [
    { label: 'Status', value: <StatusCell status={status} error={error ?? undefined} /> },
    { label: 'Hashrate', value: <NumberCell value={fmtNum(hashrate, 2)} unit="TH/s" /> },
    { label: 'Power', value: <NumberCell value={fmtNum(power, 1)} unit="W" /> },
    { label: 'Efficiency', value: <NumberCell value={eff ? fmtNum(eff, 1) : '—'} unit="W/TH" /> },
    {
      label: 'Max chip temp',
      value: <NumberCell value={fmtNum(tempChip, 1)} unit="°C" tone={tempTone(tempChip)} />,
    },
  ];
  if (tempAvg !== null && tempAvg !== undefined) {
    rows.push({
      label: 'Average chip temp',
      value: <NumberCell value={fmtNum(tempAvg, 1)} unit="°C" tone={tempTone(tempAvg)} />,
    });
  }
  rows.push({
    label: vrLabel,
    value: <NumberCell value={fmtNum(tempVr, 1)} unit="°C" tone={tempTone(tempVr)} />,
  });
  if (tempIn !== null && tempIn !== undefined) {
    rows.push({
      label: 'Air inlet temp',
      value: <NumberCell value={fmtNum(tempIn, 1)} unit="°C" />,
    });
  }
  if (family !== 'canaan' && tempOut !== null && tempOut !== undefined) {
    rows.push({
      label: 'Air outlet temp',
      value: <NumberCell value={fmtNum(tempOut, 1)} unit="°C" />,
    });
  }
  // Fan rendering:
  //  - If the driver populated the structured `fans` list (LuxOS today,
  //    others in the future), render one tile per fan with its RPM,
  //    duty cycle and connector hint. This gracefully handles any
  //    number of fans — 4 on an Antminer S19, 2 on a NerdOctaxe, etc.
  //  - Otherwise fall back to the legacy single-fan / NerdOctaxe-Fan2
  //    rendering so non-LuxOS drivers stay untouched.
  const structuredFans = ls?.fans ?? [];
  if (structuredFans.length > 0) {
    structuredFans.forEach((f, idx) => {
      const pctSuffix =
        f.speed_pct !== null && f.speed_pct !== undefined ? ` · ${fmtNum(f.speed_pct, 0)}%` : '';
      // LuxOS exposes only a fan ID (0-3) and a physical connector
      // string ("J12 | J14"). The connector doesn't match the logical
      // fan number, which made the old "Fan 1 (fan4)" labels confusing.
      // Map the fan ID to its physical position instead (J15/J14 = front
      // pair, J13/J12 = rear pair per Bitmain S19 wiring) and keep the
      // raw connector available as a hover tooltip for verification.
      const fanId = f.id ?? idx;
      const label =
        family === 'luxos'
          ? `Fan · ${luxosFanPosition(fanId)}`
          : structuredFans.length > 1
            ? `Fan ${idx + 1}`
            : 'Fan';
      rows.push({
        label,
        title: f.connector ? `Connector: ${f.connector}` : undefined,
        value: (
          <NumberCell
            value={f.rpm !== null && f.rpm !== undefined ? String(f.rpm) : '—'}
            unit={`rpm${pctSuffix}`}
          />
        ),
      });
    });
  } else {
    // On NerdOctaxe the two fans sit on different connectors: the primary
    // fan (`fanrpm`) is the CPU/ASIC fan on connector C2 (lower) and the
    // secondary fan (`fanrpm2`) is the Aux/VRM fan on connector C1 (upper).
    // Label them by role + connector so they read clearly. On single-fan
    // miners we keep the original generic label.
    const isNerdOctaxeDualFan = isNerdOctaxe && fanRpm2 !== null;
    const primaryFanLabel = isNerdOctaxeDualFan ? 'CPU fan (C2 lower)' : 'Fan';
    rows.push({
      label: primaryFanLabel,
      title: isNerdOctaxeDualFan ? 'Connector: C2 (lower)' : undefined,
      value: (
        <NumberCell
          value={fanRpm !== null && fanRpm !== undefined ? String(fanRpm) : '—'}
          unit={`rpm${fanPct !== null && fanPct !== undefined ? ` · ${fmtNum(fanPct, 0)}%` : ''}`}
        />
      ),
    });
    // NerdOctaxe second fan (only when populated) — Aux/VRM fan on C1 (upper).
    if (fanRpm2 !== null && fanRpm2 !== undefined) {
      // The C1/upper header is PWM-driven (firmware reports `fanspeed2`)
      // but often has no tachometer feedback, so `fanrpm2` comes back as
      // 0. That 0 is "no RPM reading", not "fan stopped" — render it as
      // "—" so it reads as missing data rather than a stalled fan, while
      // still showing the duty-cycle %.
      rows.push({
        label: 'Aux/VRM fan (C1 upper)',
        title: 'Connector: C1 (upper)',
        value: (
          <NumberCell
            value={fanRpm2 ? String(fanRpm2) : '—'}
            unit={`rpm${fanPct2 !== null && fanPct2 !== undefined ? ` · ${fmtNum(fanPct2, 0)}%` : ''}`}
          />
        ),
      });
    }
  }
  // NerdOctaxe PSU current draw (only when populated).
  if (currentA !== null && currentA !== undefined) {
    rows.push({
      label: 'PSU current',
      value: <NumberCell value={fmtNum(currentA, 2)} unit="A" />,
    });
  }
  rows.push(
    {
      // On multi-board LuxOS miners the single value is the average of
      // the per-board frequencies — label it as such so it's not read
      // as a single chip/board reading.
      label: family === 'luxos' && boardCount > 1 ? 'Frequency (avg)' : 'Frequency',
      value: <NumberCell value={freq ? `${fmtNum(freq, 0)}` : '—'} unit="MHz" />,
    },
    { label: 'Voltage', value: <NumberCell value={volt ? String(volt) : '—'} unit="mV" /> },
    {
      // ASIC count: prefer the true chip count when the driver
      // separates it from the board count (LuxOS today). Older
      // drivers leave chip_count/board_count null, in which case we
      // fall back to whatever's in asic_count just like before.
      label: 'ASIC count',
      value: (
        <NumberCell
          value={
            ls?.chip_count
              ? String(ls.chip_count)
              : ls?.asic_count
                ? String(ls.asic_count)
                : '—'
          }
          unit={
            ls?.board_count && ls.board_count > 1
              ? `chips · ${ls.board_count} boards`
              : ''
          }
        />
      ),
    },
    { label: 'Uptime', value: <NumberCell value={fmtUptime(uptime)} unit="" /> },
    {
      label: 'Accepted / Rejected',
      value: (
        <NumberCell
          value={`${accepted ?? '—'} / ${rejected ?? '—'}`}
          unit=""
        />
      ),
    },
  );
  // Rejection rate — derived from accepted/rejected, so it's available
  // on every family (computed even when hw counters are still null on a
  // freshly booted device). 2 decimal places.
  if (rejectionRate !== null) {
    // On NerdOctaxe this doubles as the "HW error %": the firmware sends
    // duplicate HW nonces to the pool and counts them as rejected, so the
    // reject rate already includes them. This mirrors what NerdOS shows
    // (it has no standalone HW%). Label + tooltip make the percentage form
    // of the HW errors obvious on the NerdOctaxe view.
    rows.push({
      label: isNerdOctaxe ? 'HW / reject rate' : 'Rejection rate',
      title: isNerdOctaxe
        ? 'Rejected shares as a % of total submitted: rejected / (accepted + rejected). This is the percentage form of the HW errors — the firmware sends duplicate HW nonces to the pool and counts them as rejected, so they are included here (same metric NerdOS shows).'
        : undefined,
      value: <NumberCell value={fmtNum(rejectionRate, 2)} unit="%" />,
    });
  }
  // Hardware error rate (%) — LuxOS computes this server-side from the
  // Hardware Errors / Diff1 Work counters (its native Device Hardware%
  // is hard-coded to 0). 2 decimal places.
  if (hwErrorRate !== null) {
    rows.push({
      label: 'HW error rate',
      value: <NumberCell value={fmtNum(hwErrorRate, 2)} unit="%" />,
    });
  }
  // Raw aggregate HW error counter — NerdOctaxe only (firmware exposes
  // `duplicateHWNonces`; nothing equivalent on classic Bitaxe). Its
  // percentage form is the HW / reject rate row above.
  if (isNerdOctaxe && hwErrors !== null && hwErrors !== undefined) {
    rows.push({
      label: 'HW errors (count)',
      title:
        'Raw duplicate-HW-nonce counter reported by the firmware (duplicateHWNonces). Its percentage form is the HW / reject rate above.',
      value: <NumberCell value={String(hwErrors)} unit="" />,
    });
  }
  rows.push({
    label: 'Best difficulty (session)',
    value: <NumberCell value={bestSession ? fmtDifficulty(bestSession) : '—'} unit="" />,
  });
  // Pool config — show only on NerdOctaxe, since the dual-pool view
  // doesn't make sense on the other families. We show the primary
  // pool always and the fallback only when configured. `pool_active`
  // gets a small badge so the user can tell at a glance which one is
  // currently mining.
  if (isNerdOctaxe) {
    rows.push({
      label: 'Pool (primary)',
      value: <PoolCell url={poolUrl} worker={worker} active={poolActive === 'primary'} />,
    });
    if (poolUrlFallback) {
      rows.push({
        label: 'Pool (fallback)',
        value: (
          <PoolCell url={poolUrlFallback} worker={workerFallback} active={poolActive === 'fallback'} />
        ),
      });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Current status</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3 md:grid-cols-4">
          {rows.map((r) => (
            <div key={r.label} className="flex flex-col">
              <span
                className={cn(
                  'text-[10px] uppercase tracking-wider text-muted-foreground',
                  r.title && 'cursor-help',
                )}
                title={r.title}
              >
                {r.label}
              </span>
              <span className="text-sm font-semibold tabular-nums">{r.value}</span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// LuxOS fan ID → physical position. The `fans` API gives only an ID
// (0-3) and a connector string; it carries no position metadata. Per
// Bitmain S19 wiring, connectors J15/J14 are the front fan pair and
// J13/J12 the rear pair, which map to fan IDs 0/1 (front) and 2/3
// (rear); within each pair we take the lower ID as the top fan. The
// raw connector stays visible as a tooltip so the mapping is verifiable
// on the physical unit. Out-of-range IDs fall back to "Fan N".
const LUXOS_FAN_POSITIONS = ['Top Front', 'Bottom Front', 'Top Back', 'Bottom Back'];

function luxosFanPosition(id: number): string {
  return LUXOS_FAN_POSITIONS[id] ?? `Fan ${id + 1}`;
}

function StatusCell({ status, error }: { status: string; error?: string }) {
  const colour =
    status === 'online' ? 'text-emerald-400'
    : status === 'offline' ? 'text-destructive'
    : 'text-muted-foreground';
  return (
    <span className={colour}>
      <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-current align-middle" />
      {status}
      {error && <span className="ml-2 text-xs font-normal text-muted-foreground">· {error}</span>}
    </span>
  );
}

interface NumberCellProps {
  value: string;
  unit: string;
  tone?: ReturnType<typeof tempTone>;
}

/**
 * Tile that renders a pool config (URL + worker) with an optional
 * "active" dot. Used by the NerdOctaxe-only Pool (primary)/(fallback)
 * rows in the LiveStats grid. The text intentionally renders in two
 * lines because pool URLs and worker names are both long enough that
 * cramming them onto one line truncates one or the other.
 */
function PoolCell({
  url,
  worker,
  active,
}: {
  url: string | null;
  worker: string | null;
  active?: boolean;
}) {
  if (!url && !worker) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span className="flex flex-col">
      <span className="flex items-center gap-1.5 truncate text-foreground">
        {active && (
          <span
            className="inline-block h-1.5 w-1.5 rounded-full bg-emerald-400"
            title="Currently mining on this pool"
          />
        )}
        <span className="truncate font-normal" title={url ?? undefined}>
          {url ?? '—'}
        </span>
      </span>
      {worker && (
        <span
          className="truncate text-[10px] font-normal text-muted-foreground"
          title={worker}
        >
          {worker}
        </span>
      )}
    </span>
  );
}

function NumberCell({ value, unit, tone }: NumberCellProps) {
  const toneCls =
    tone === 'critical' ? 'text-destructive'
    : tone === 'hot' ? 'text-orange-400'
    : tone === 'warm' ? 'text-amber-400'
    : 'text-foreground';
  return (
    <span className={cn('inline-flex items-baseline gap-1', toneCls)}>
      {value}
      {unit && <span className="text-[10px] font-normal text-muted-foreground">{unit}</span>}
    </span>
  );
}
