import { Link } from 'react-router-dom';

import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { fmtNum, fmtRelative, fmtUptime, tempTone, FAMILY_LABEL } from '@/lib/format';
import { cn } from '@/lib/utils';
import type { MinerListEntry } from '@/lib/types';

interface Props {
  miner: MinerListEntry;
}

/**
 * One card in the fleet grid. Whole card is a link to /miner/:id —
 * same behaviour as the vanilla dashboard.
 */
export function MinerCard({ miner }: Props) {
  const lm = miner.last_metric;

  // Status: live state first (poller verdict), then DB last_status as fallback
  const status: 'online' | 'offline' | 'pending' = miner.live_online === false
    ? 'offline'
    : miner.live_online === true
      ? 'online'
      : (miner.last_status as 'pending' | undefined) ?? 'pending';

  // When the miner is offline we no longer have live readings, so show a
  // "—" placeholder instead of the last values we saw while it was up
  // (those are stale and read as if the device were still running). We
  // gate specifically on "offline" — not "pending" — so a freshly loaded
  // page before the first poll still shows the last known metrics.
  const offline = status === 'offline';

  const familyLabel = FAMILY_LABEL[miner.family] ?? miner.family;

  return (
    <Link
      to={`/miner/${miner.id}`}
      className="block rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
    >
      <Card className="h-full p-4 transition-colors hover:bg-card/80 hover:border-border-strong">
        <header className="flex items-start justify-between gap-3">
          {/* Left gutter (pl-7) reserves room for the drag handle drawn
              by SortableMinerCard. The handle is 28 px wide and sits at
              left-2; pl-7 (1.75 rem) lines the name up just to its
              right, so visually they read as "[handle] miner name". */}
          <div className="min-w-0 pl-7">
            <div className="truncate font-semibold">{miner.name}</div>
            <div className="truncate text-xs text-muted-foreground">
              {familyLabel} · {miner.host}
            </div>
          </div>
          <StatusBadge status={status} />
        </header>

        <div className="mt-4 grid grid-cols-3 gap-3">
          <Metric label="Hashrate" value={offline ? '—' : fmtNum(lm?.hashrate_ths, 2)} unit="TH/s" />
          <Metric label="Power" value={offline ? '—' : fmtNum(lm?.power_w, 0)} unit="W" />
          <Metric
            label="Chip"
            value={offline ? '—' : fmtNum(lm?.temp_chip_c, 1)}
            unit="°C"
            tone={offline ? undefined : tempTone(lm?.temp_chip_c)}
          />
          <Metric
            label="VR"
            value={offline ? '—' : fmtNum(lm?.temp_vr_c, 1)}
            unit="°C"
            tone={offline ? undefined : tempTone(lm?.temp_vr_c)}
          />
          <Metric label="Fan" value={offline ? '—' : lm?.fan_rpm ? String(lm.fan_rpm) : '—'} unit="rpm" />
          <Metric label="Uptime" value={offline ? '—' : fmtUptime(lm?.uptime_s)} unit="" />
        </div>

        <footer className="mt-4 flex items-center justify-between border-t border-border pt-3 text-[11px] text-muted-foreground">
          <span className="truncate">{miner.model ?? ''}</span>
          <span>{fmtRelative(lm?.ts)}</span>
        </footer>
      </Card>
    </Link>
  );
}

function StatusBadge({ status }: { status: 'online' | 'offline' | 'pending' }) {
  if (status === 'online') {
    return (
      <Badge variant="success" className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
        online
      </Badge>
    );
  }
  if (status === 'offline') {
    return (
      <Badge variant="danger" className="flex items-center gap-1">
        <span className="h-1.5 w-1.5 rounded-full bg-red-400" />
        offline
      </Badge>
    );
  }
  return (
    <Badge variant="outline" className="flex items-center gap-1">
      <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground" />
      {status}
    </Badge>
  );
}

interface MetricProps {
  label: string;
  value: string;
  unit: string;
  tone?: ReturnType<typeof tempTone>;
}

function Metric({ label, value, unit, tone }: MetricProps) {
  const toneCls =
    tone === 'critical' ? 'text-destructive'
    : tone === 'hot' ? 'text-orange-400'
    : tone === 'warm' ? 'text-amber-400'
    : 'text-foreground';
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className={cn('text-sm font-semibold tabular-nums', toneCls)}>
        {value}
        {unit && <span className="ml-1 text-[10px] font-normal text-muted-foreground">{unit}</span>}
      </span>
    </div>
  );
}
