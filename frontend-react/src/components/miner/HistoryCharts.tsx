import { useEffect, useMemo, useRef, useState } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { fmtNum, isCanaanFamily } from '@/lib/format';
import {
  useAmbientHistory,
  useAmbientTemp,
  useMinerMetrics,
  useSetAmbientSensor,
} from '@/api/hooks';

interface Props {
  minerId: number;
  /** Miner family — Canaan/Avalon relabels the VR series: those boards
   *  have no VR sensor, the driver feeds the air outlet temp into temp_vr_c. */
  family?: string;
  /** Ambient sensor (room) assigned to this miner, or null. Its stored
   *  series is overlaid on the Temperature chart; null draws no line. */
  ambientSensorId?: string | null;
  /** Cached display name of the assigned sensor, used for the overlay label
   *  and the picker even when that sensor is offline. */
  ambientSensorName?: string | null;
}

const RANGES: Array<{ label: string; seconds: number }> = [
  { label: '1h', seconds: 3600 },
  { label: '6h', seconds: 21600 },
  { label: '24h', seconds: 86400 },
  { label: '7d', seconds: 604800 },
  { label: '30d', seconds: 2592000 },
];

// The trailing edge of the window (`now`) is quantised to this many seconds.
// Re-renders within the same bucket reuse the exact same fromTs/toTs, so the
// React Query keys stay referentially stable and the charts never refetch
// (let alone blank) just because a 5s fleet poll re-rendered the page.
const BUCKET_S = 30;
// How often we look at the clock to roll the bucket forward. Setting state to
// the same bucket value is a no-op in React, so a real refetch happens at most
// once per bucket no matter how fast this ticks.
const TICK_MS = 15_000;

const bucketedNow = () => Math.floor(Date.now() / 1000 / BUCKET_S) * BUCKET_S;

// A shared, theme-aware vertical guide under the cursor — replaces Recharts'
// default solid grey line so the hover reads as part of the design.
const TOOLTIP_CURSOR = {
  stroke: 'hsl(var(--muted-foreground))',
  strokeWidth: 1,
  strokeDasharray: '4 4',
};

const TOOLTIP_CONTENT_STYLE = {
  background: 'hsl(var(--card))',
  border: '1px solid hsl(var(--border))',
  borderRadius: 8,
  fontSize: 12,
};

/**
 * History tab: hashrate and temperature time-series over a selectable
 * range. Powered by Recharts so axes, tooltip, and responsiveness come
 * for free. Range selector mirrors the vanilla one.
 */
export function HistoryCharts({ minerId, family, ambientSensorId, ambientSensorName }: Props) {
  const [range, setRange] = useState(86400);
  const vrSeriesLabel = isCanaanFamily(family) ? 'Air out' : 'VR';

  // The visible window is [now - range, now]. We hold `now` in state and only
  // advance it on a fixed cadence instead of recomputing it every render —
  // otherwise fromTs/toTs (and the React Query keys derived from them) would
  // change on every 5s poll re-render, refetching the whole range, dropping
  // the charts to a skeleton, and killing whatever tooltip the user was
  // reading. A steady, bucketed `now` keeps the keys stable so the charts
  // stay mounted and only pick up new points when the bucket rolls forward.
  const [nowTs, setNowTs] = useState(bucketedNow);

  // True while the pointer is over the charts. We freeze the window during a
  // hover so the data can't shift out from under the tooltip mid-read; on
  // leave we snap straight back to live. A ref (not state) so flipping it
  // never triggers a render of its own.
  const hoveringRef = useRef(false);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (hoveringRef.current) return;
      setNowTs(bucketedNow());
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  const onChartsEnter = () => {
    hoveringRef.current = true;
  };
  const onChartsLeave = () => {
    hoveringRef.current = false;
    setNowTs(bucketedNow());
  };

  const fromTs = nowTs - range;
  const toTs = nowTs;

  const { data, isLoading } = useMinerMetrics(minerId, fromTs, toTs);

  // Ambient overlay: the stored series of the room sensor assigned to this
  // miner — none assigned → no line. The live snapshot supplies the picker's
  // sensor names and resolves the assigned sensor's label for the tooltip.
  const { data: ambientData } = useAmbientHistory(fromTs, toTs, ambientSensorId);
  const { data: ambientFleet } = useAmbientTemp();
  const setAmbientSensor = useSetAmbientSensor(minerId);

  // Overlay label uses the miner's cached room name (always available, even
  // when the sensor is offline); falls back to a generic label otherwise.
  const roomName = ambientSensorName ?? 'Room';

  const sensorOptions = useMemo(() => {
    const opts = (ambientFleet?.sensors ?? []).map((s) => ({
      id: s.sensor_id,
      label: s.name,
    }));
    // Keep the assigned sensor selectable even while it is offline, labelled
    // by its cached name instead of the raw id.
    if (ambientSensorId && !opts.some((o) => o.id === ambientSensorId)) {
      opts.push({
        id: ambientSensorId,
        label: `${ambientSensorName ?? ambientSensorId} (offline)`,
      });
    }
    return opts;
  }, [ambientFleet, ambientSensorId, ambientSensorName]);

  const roomSelector = (
    <select
      value={ambientSensorId ?? ''}
      onChange={(e) => {
        const id = e.target.value || null;
        const name = id
          ? (ambientFleet?.sensors.find((s) => s.sensor_id === id)?.name ?? null)
          : null;
        setAmbientSensor.mutate({ sensorId: id, name });
      }}
      title="Room sensor overlaid on this chart"
      className="h-6 rounded-md border border-border bg-card px-1.5 text-[11px] text-foreground"
    >
      <option value="">No room</option>
      {sensorOptions.map((o) => (
        <option key={o.id} value={o.id}>
          {o.label}
        </option>
      ))}
    </select>
  );

  const series = useMemo(() => {
    const rows = data?.metrics ?? [];
    // Reject rate is derived on the fly from the cumulative accepted /
    // rejected counters that are already stored in the metrics tables —
    // no extra backend column needed. We compute it *delta-based* between
    // consecutive points (Δrejected / (Δaccepted + Δrejected)) so the line
    // reflects what happened in each window rather than a flat lifetime
    // average. On NerdOctaxe this is also the "HW error %": duplicate HW
    // nonces are submitted to the pool and counted as rejected, so they're
    // included here. Negative deltas (counter reset on a miner restart)
    // and empty windows (no shares) produce a null → a gap in the line.
    type Point = {
      ts: number;
      hashrate: number | null;
      tempChip: number | null;
      tempVr: number | null;
      rejectPct: number | null;
    };
    const out: Point[] = [];
    let prevAcc: number | null = null;
    let prevRej: number | null = null;
    for (const p of rows) {
      const acc = p.accepted;
      const rej = p.rejected;
      let rejectPct: number | null = null;
      if (acc !== null && rej !== null && prevAcc !== null && prevRej !== null) {
        const dAcc = acc - prevAcc;
        const dRej = rej - prevRej;
        if (dAcc >= 0 && dRej >= 0 && dAcc + dRej > 0) {
          rejectPct = (dRej / (dAcc + dRej)) * 100;
        }
      }
      if (acc !== null) prevAcc = acc;
      if (rej !== null) prevRej = rej;
      out.push({
        ts: p.ts * 1000,
        hashrate: p.hashrate_ths,
        tempChip: p.temp_chip_c,
        tempVr: p.temp_vr_c,
        rejectPct,
      });
    }
    return out;
  }, [data]);

  const hasReject = useMemo(
    () => series.some((p) => p.rejectPct !== null),
    [series],
  );

  // Temperature chart dataset: the per-miner chip/VR series merged with the
  // fleet ambient series on a shared time axis. Both are sampled on the same
  // poll cadence (raw tier) or bucketed to the same minute/hour boundaries
  // (rollup tiers), so timestamps align; we still merge by ts defensively so
  // a cycle present in only one series (e.g. miner offline but room sensor
  // alive) keeps the other line. Missing values stay null → a gap, never a
  // fabricated zero.
  const tempSeries = useMemo(() => {
    type TPoint = {
      ts: number;
      tempChip: number | null;
      tempVr: number | null;
      tempAmbient: number | null;
    };
    const byTs = new Map<number, TPoint>();
    for (const p of series) {
      byTs.set(p.ts, { ts: p.ts, tempChip: p.tempChip, tempVr: p.tempVr, tempAmbient: null });
    }
    for (const a of ambientData?.points ?? []) {
      const ts = a.ts * 1000;
      const existing = byTs.get(ts);
      if (existing) existing.tempAmbient = a.temp_c;
      else byTs.set(ts, { ts, tempChip: null, tempVr: null, tempAmbient: a.temp_c });
    }
    return Array.from(byTs.values()).sort((x, y) => x.ts - y.ts);
  }, [series, ambientData]);

  const hasAmbient = useMemo(
    () => tempSeries.some((p) => p.tempAmbient !== null),
    [tempSeries],
  );

  const tickFormat = (ts: number) => {
    const d = new Date(ts);
    if (range <= 3600) {
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    }
    if (range <= 86400) {
      return `${String(d.getHours()).padStart(2, '0')}:00`;
    }
    return `${d.getMonth() + 1}/${d.getDate()}`;
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base">History</CardTitle>
        <div className="flex gap-1 rounded-lg border border-border bg-card p-1">
          {RANGES.map((r) => (
            <Button
              key={r.label}
              size="sm"
              variant={range === r.seconds ? 'default' : 'ghost'}
              className="h-7 px-2.5 text-xs"
              onClick={() => setRange(r.seconds)}
            >
              {r.label}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-4" onMouseEnter={onChartsEnter} onMouseLeave={onChartsLeave}>
        <ChartBlock title="Hashrate" unit="TH/s" isLoading={isLoading} hasData={!!series.length}>
          <LineChart
            data={series}
            margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
            syncId="mw-history"
            syncMethod="value"
          >
            <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              domain={['dataMin', 'dataMax']}
              tickFormatter={tickFormat}
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickMargin={6}
            />
            <YAxis
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              width={36}
              tickFormatter={(v) => fmtNum(v, 1)}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              cursor={TOOLTIP_CURSOR}
              labelFormatter={(ts) => new Date(ts as number).toLocaleString()}
              formatter={(v: number) => [`${fmtNum(v, 2)} TH/s`, 'Hashrate']}
            />
            <Line
              type="monotone"
              dataKey="hashrate"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ChartBlock>

        <ChartBlock
          title="Temperature"
          unit="°C"
          isLoading={isLoading}
          hasData={!!tempSeries.length}
          action={roomSelector}
        >
          <LineChart
            data={tempSeries}
            margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
            syncId="mw-history"
            syncMethod="value"
          >
            <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              domain={['dataMin', 'dataMax']}
              tickFormatter={tickFormat}
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickMargin={6}
            />
            <YAxis
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              width={36}
              tickFormatter={(v) => fmtNum(v, 0)}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              cursor={TOOLTIP_CURSOR}
              labelFormatter={(ts) => new Date(ts as number).toLocaleString()}
              formatter={(v: number, name) => [`${fmtNum(v, 1)} °C`, name as string]}
            />
            <Legend
              verticalAlign="top"
              align="right"
              height={22}
              iconType="plainline"
              iconSize={12}
              wrapperStyle={{ fontSize: 11 }}
            />
            <Line type="monotone" dataKey="tempChip" name="Chip" stroke="#fb923c" strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            <Line type="monotone" dataKey="tempVr" name={vrSeriesLabel} stroke="#facc15" strokeWidth={2} dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
            {/* Room temperature relayed over HTTP — only when the relay has
                data. Dashed cool-blue to read as "environment, not device",
                and connectNulls so a brief relay gap doesn't fragment it. */}
            {hasAmbient && (
              <Line
                type="monotone"
                dataKey="tempAmbient"
                name={roomName}
                stroke="#38bdf8"
                strokeWidth={2}
                strokeDasharray="4 3"
                dot={false}
                activeDot={{ r: 4 }}
                isAnimationActive={false}
                connectNulls
              />
            )}
          </LineChart>
        </ChartBlock>

        {/* Reject rate (a.k.a. HW error % on NerdOctaxe). Derived from the
            stored accepted/rejected counters, so full history is available
            retroactively. `hasData` also requires at least one computable
            point so we don't show an empty axis for a range with no shares. */}
        <ChartBlock
          title="Reject rate"
          unit="%"
          isLoading={isLoading}
          hasData={!!series.length && hasReject}
        >
          <LineChart
            data={series}
            margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
            syncId="mw-history"
            syncMethod="value"
          >
            <CartesianGrid stroke="hsl(var(--border))" strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              domain={['dataMin', 'dataMax']}
              tickFormatter={tickFormat}
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickMargin={6}
            />
            <YAxis
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              width={36}
              domain={[0, 'auto']}
              tickFormatter={(v) => fmtNum(v, 2)}
            />
            <Tooltip
              contentStyle={TOOLTIP_CONTENT_STYLE}
              cursor={TOOLTIP_CURSOR}
              labelFormatter={(ts) => new Date(ts as number).toLocaleString()}
              formatter={(v: number) => [`${fmtNum(v, 3)} %`, 'Reject rate']}
            />
            <Line
              type="monotone"
              dataKey="rejectPct"
              stroke="#f87171"
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
              connectNulls={false}
            />
          </LineChart>
        </ChartBlock>
      </CardContent>
    </Card>
  );
}

interface ChartBlockProps {
  title: string;
  unit: string;
  isLoading: boolean;
  hasData: boolean;
  children: React.ReactElement;
  /** Optional control rendered in the block header (e.g. the room picker). */
  action?: React.ReactNode;
}

function ChartBlock({ title, unit, isLoading, hasData, children, action }: ChartBlockProps) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          {title}
        </span>
        <div className="flex items-center gap-2">
          {action}
          <span className="text-[11px] text-muted-foreground">{unit}</span>
        </div>
      </div>
      <div className="h-56 w-full">
        {isLoading && !hasData ? (
          <Skeleton className="h-full w-full" />
        ) : !hasData ? (
          <div className="flex h-full items-center justify-center rounded-md border border-dashed border-border text-sm text-muted-foreground">
            No data in this range
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {children}
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
