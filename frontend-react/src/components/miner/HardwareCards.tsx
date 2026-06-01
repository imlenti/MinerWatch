import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { fmtNum, fmtUptime, tempTone, FAMILY_LABEL } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { MinerDetailResponse } from '@/lib/types';

interface Props {
  data: MinerDetailResponse;
}

type Row = { label: string; value: React.ReactNode | null };

/**
 * Five grouped read-only cards replacing the flat <table> of the
 * vanilla hardware view. Null rows are dropped so devices with
 * sparser drivers stay dense, not sprinkled with "—".
 */
export function HardwareCards({ data }: Props) {
  const { miner, last_metric: lm, live_sample: ls } = data;
  const raw = (ls?.raw ?? {}) as Record<string, unknown>;

  // Live poller verdict first, DB last_status as fallback. When offline
  // we drop the live readings (temps, fan, power, hashrate, shares…) and
  // show nothing for them rather than echoing the stale last-seen values
  // — identity fields (family, model, host, MAC) and user setpoints come
  // from the miner record and stay visible.
  const offline = ls ? ls.online === false : miner.last_status === 'offline';

  // Prefer the live sample (fresher, more fields) and fall back to the
  // latest DB row when the live one hasn't arrived yet. The helper
  // returns `unknown` and callers `as`-cast at the point of use — this
  // is much easier to maintain than a generic keyed lookup that has to
  // satisfy both shapes' indexers at the type level.
  const liveBag = (ls ?? {}) as unknown as Record<string, unknown>;
  const dbBag = (lm ?? {}) as unknown as Record<string, unknown>;
  const v = (key: string): unknown => {
    if (offline) return null;
    const lv = liveBag[key];
    if (lv !== null && lv !== undefined) return lv;
    return dbBag[key];
  };

  // ----- Identity -----
  const identity: Row[] = [
    { label: 'Family', value: FAMILY_LABEL[miner.family] ?? miner.family },
    {
      // Fall back to the live sample's model when the stored record has
      // none — LuxOS leaves miner.model unset (to avoid discovery false
      // positives) but the driver parses the real model ("Antminer
      // S19j Pro") fresh on every poll.
      label: 'Model',
      value: miner.model ?? ls?.model ?? (raw.ASICModel as string | undefined) ?? '—',
    },
    {
      // ASIC chip model — LuxOS derives it from the model name
      // (ls.chip_model); AxeOS firmware reports it directly as ASICModel.
      label: 'Chip model',
      value: ls?.chip_model ?? (raw.ASICModel as string | undefined) ?? null,
    },
    {
      label: 'Board version',
      value: raw.boardVersion !== undefined ? String(raw.boardVersion) : null,
    },
    {
      label: 'Firmware',
      value: (ls?.firmware_version ?? raw.version ?? raw.firmwareVersion ?? null) as string | null,
    },
    {
      label: 'Hostname',
      value: ls?.hostname ?? (raw.hostname as string | undefined) ?? null,
    },
    {
      label: 'MAC',
      value: miner.mac ? <Code>{miner.mac}</Code> : null,
    },
    {
      label: 'Host',
      value: <Code>{miner.host}{miner.port ? `:${miner.port}` : ''}</Code>,
    },
    {
      label: 'WiFi RSSI',
      value: raw.wifiRSSI !== undefined && raw.wifiRSSI !== null ? `${raw.wifiRSSI} dBm` : null,
    },
    { label: 'Notes', value: miner.notes || null },
  ];

  // ----- ASIC -----
  const freq = v('frequency_mhz') as number | null | undefined;
  const volt = v('voltage_mv') as number | null | undefined;
  const voltReq = raw.coreVoltage as number | undefined;
  const voltAct = (raw.coreVoltageActual as number | undefined) ?? volt;
  let voltageCell: React.ReactNode | null = null;
  if (voltReq && voltAct && Number(voltReq) !== Number(voltAct)) {
    voltageCell = (
      <span>
        {voltAct} mV
        <span className="ml-1 text-[10px] text-muted-foreground">(set {voltReq} mV)</span>
      </span>
    );
  } else if (volt) {
    voltageCell = `${volt} mV`;
  }
  const asic: Row[] = [
    { label: 'Frequency', value: freq ? `${freq} MHz` : null },
    { label: 'Core voltage', value: voltageCell },
    { label: 'ASIC count', value: ls?.asic_count ?? (raw.asicCount as number | undefined) ?? null },
    {
      label: 'Expected hashrate',
      value: raw.expectedHashrate ? `${raw.expectedHashrate} GH/s` : null,
    },
    {
      label: 'Small core count',
      value: raw.smallCoreCount !== undefined ? String(raw.smallCoreCount) : null,
    },
    {
      // Hardware error rate (%), 2 decimals. LuxOS only (computed from
      // Hardware Errors / Diff1 Work, since its native Device Hardware%
      // is always 0); null elsewhere so the row is dropped.
      label: 'HW error rate',
      value:
        ls?.hw_error_rate !== null && ls?.hw_error_rate !== undefined
          ? `${fmtNum(ls.hw_error_rate, 2)} %`
          : null,
    },
    {
      label: 'Overheat mode',
      value:
        raw.overheat_mode !== undefined
          ? raw.overheat_mode
            ? <Badge variant="danger">Triggered</Badge>
            : <Badge variant="success">Normal</Badge>
          : null,
    },
  ];

  // ----- Thermal -----
  const tChip = v('temp_chip_c') as number | null;
  // Second chip sensor — populated only by multi-ASIC AxeOS boards
  // (Bitaxe SupraHex). When present we render the two sensors separately.
  const tChip2 = ls?.temp_chip_2_c ?? null;
  const tVr = v('temp_vr_c') as number | null;
  const tIn = ls?.temp_inlet_c ?? null;
  const tOut = ls?.temp_outlet_c ?? null;
  const tAvg = ls?.temp_avg_c ?? null;
  const target = miner.auto_target_c ?? (raw.temptarget as number | undefined) ?? null;
  const tempCell = (t: number | null | undefined) =>
    t !== null && t !== undefined ? <TempVal value={t} /> : null;
  // On a two-sensor board, ``temp_chip_c`` is the *max* of the two sensors
  // (it's what the alert + auto-fan track), so showing it as "Max chip
  // temp" would just duplicate the hotter row. Instead we show sensor 1
  // (the raw ``temp``) and sensor 2 side by side, and drop the "Max" row.
  // Single-sensor devices keep the familiar "Max chip temp" label, so
  // nothing changes for Gamma/Supra-class boards with one ASIC.
  const tChip1raw =
    raw.temp !== undefined && raw.temp !== null && Number(raw.temp) > 0
      ? Number(raw.temp)
      : null;
  const chipTempRows: Row[] =
    tChip2 !== null && tChip2 !== undefined
      ? [
          { label: 'Chip temp 1', value: tempCell(tChip1raw ?? tChip) },
          { label: 'Chip temp 2', value: tempCell(tChip2) },
        ]
      : [{ label: 'Max chip temp', value: tempCell(tChip) }];
  const thermal: Row[] = [
    ...chipTempRows,
    { label: 'Average chip temp', value: tempCell(tAvg) },
    { label: miner.family === 'canaan' ? 'Air outlet temp' : 'VR temp', value: tempCell(tVr) },
    { label: 'Air inlet temp', value: tempCell(tIn) },
    {
      label: 'Air outlet temp',
      value: miner.family !== 'canaan' ? tempCell(tOut) : null,
    },
    { label: 'Target temp', value: target ? `${fmtNum(target, 1)} °C` : null },
  ];

  // ----- Fan & Power -----
  const fanRpm = v('fan_rpm') as number | null;
  const fanPct = v('fan_pct') as number | null;
  const fanMode = miner.fan_mode ?? (raw.autofanspeed === 1 ? 'firmware' : null);
  const fanModeLabel = fanMode === 'manual'
    ? '✋ Manual'
    : fanMode === 'minerwatch'
      ? '🎯 AUTO (MinerWatch)'
      : fanMode === 'firmware'
        ? '🤖 AUTO (firmware)'
        : null;
  const power = v('power_w') as number | null;
  const hashrate = v('hashrate_ths') as number | null;
  const eff = power && hashrate ? power / hashrate : null;
  const fanPower: Row[] = [
    {
      label: 'Fan speed',
      value: fanPct !== null && fanPct !== undefined ? `${fmtNum(fanPct, 0)} %` : null,
    },
    { label: 'Fan RPM', value: fanRpm ?? null },
    { label: 'Fan mode', value: fanModeLabel ? <Badge variant="outline">{fanModeLabel}</Badge> : null },
    { label: 'Power', value: power ? `${fmtNum(power, 1)} W` : null },
    {
      label: 'Nominal voltage',
      value: raw.nominalVoltage ? `${raw.nominalVoltage} V` : null,
    },
    {
      label: 'Measured voltage',
      value: typeof raw.voltage === 'number' ? `${fmtNum((raw.voltage as number) / 1000.0, 3)} V` : null,
    },
    { label: 'Efficiency', value: eff ? `${fmtNum(eff, 1)} W/TH` : null },
    {
      label: 'Max power',
      value: raw.maxPower ? `${raw.maxPower} W` : null,
    },
    {
      label: 'Overclock',
      value:
        raw.overclockEnabled !== undefined
          ? raw.overclockEnabled
            ? <Badge variant="warning">Enabled</Badge>
            : <Badge variant="outline">Disabled</Badge>
          : null,
    },
  ];

  // ----- Pool & worker -----
  const poolUrl = v('pool_url') as string | null;
  const worker = v('worker') as string | null;
  const uptime = v('uptime_s') as number | null;
  const accepted = v('accepted') as number | null;
  const rejected = v('rejected') as number | null;
  const pool: Row[] = [
    { label: 'Pool', value: poolUrl ?? null },
    { label: 'Worker', value: worker ?? null },
    {
      label: 'Stratum user',
      value: raw.stratumUser && raw.stratumUser !== worker ? String(raw.stratumUser) : null,
    },
    {
      label: 'Fallback pool',
      value: raw.fallbackStratumURL ? String(raw.fallbackStratumURL) : null,
    },
    {
      label: 'Accepted shares',
      value: accepted !== null && accepted !== undefined ? Number(accepted).toLocaleString() : null,
    },
    {
      label: 'Rejected shares',
      value: rejected !== null && rejected !== undefined ? Number(rejected).toLocaleString() : null,
    },
    { label: 'Uptime', value: uptime ? fmtUptime(uptime) : null },
  ];

  const sections: Array<[string, Row[]]> = [
    ['Identity', identity],
    ['ASIC configuration', asic],
    ['Thermal', thermal],
    ['Fan & Power', fanPower],
    ['Pool & Worker', pool],
  ];

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {sections.map(([title, rows]) => (
        <HardwareSection key={title} title={title} rows={rows} />
      ))}
    </div>
  );
}

interface SectionProps {
  title: string;
  rows: Row[];
}

function HardwareSection({ title, rows }: SectionProps) {
  const live = rows.filter(
    (r) => r.value !== null && r.value !== undefined && r.value !== '',
  );
  if (!live.length) return null;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-xs uppercase tracking-wider text-muted-foreground font-bold">
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent className="pt-2">
        {/*
          Two-column grid: label sized to its content, value gets the
          rest. `minmax(0,1fr)` on the value column is what lets very
          long strings (Bitcoin addresses, pool URLs) shrink instead of
          pushing the card past its container. The `break-all` on the
          <dd> spans them onto multiple lines when needed; short values
          (numbers, units) are unaffected because they fit on one line.
        */}
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-2">
          {live.map((r) => (
            <div key={r.label} className="contents">
              <dt className="text-xs text-muted-foreground self-center">{r.label}</dt>
              <dd className="min-w-0 break-all text-right text-sm font-semibold tabular-nums">{r.value}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

function Code({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded border border-border bg-muted/40 px-1.5 py-0.5 text-xs font-mono">
      {children}
    </code>
  );
}

function TempVal({ value }: { value: number }) {
  const tone = tempTone(value);
  const cls = tone === 'critical' ? 'text-destructive'
    : tone === 'hot' ? 'text-orange-400'
    : tone === 'warm' ? 'text-amber-400'
    : 'text-foreground';
  return <span className={cn('tabular-nums', cls)}>{fmtNum(value, 1)} °C</span>;
}
