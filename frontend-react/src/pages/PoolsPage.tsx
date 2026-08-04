import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowUpDown, Network } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { ApiError } from '@/lib/api';
import { fmtRelative, FAMILY_LABEL } from '@/lib/format';
import { cn } from '@/lib/utils';
import { usePools, useSetMinerCoin } from '@/api/hooks';
import type { CoinSource, MinedCoin, PoolRow } from '@/lib/types';

// Fleet-wide view of every pool configured on every miner. One row per
// (miner, pool slot) — the cgminer-family drivers contribute one row
// per pool slot they expose, AxeOS drivers contribute one row for the
// primary slot (and a second for NerdOctaxe's fallback when set).
//
// Column rationale (see the discussion in HANDOFF on why these are the
// columns rather than e.g. "Ping"):
//   * URL / User             — the configured stratum endpoint and the
//                              worker name the firmware is sending.
//   * Status                 — Alive / Dead / Unknown; cgminer-family
//                              drivers give us the explicit flag, AxeOS
//                              has no per-pool flag so we fall back to
//                              the miner's overall live_online state
//                              (no row goes red just because AxeOS
//                              doesn't report Alive/Dead).
//   * Accepted / Rejected /
//     Stale                  — raw share counters. Stale is empty for
//                              AxeOS firmwares that don't surface it.
//   * Reject %               — derived; more actionable than raw counts
//                              because uptimes differ across miners.
//   * Last share             — "X minutes ago" — cross-driver liveness
//                              signal. Empty when the firmware doesn't
//                              expose it (AxeOS family).

type SortKey =
  | 'miner'
  | 'url'
  | 'user'
  | 'coin'
  | 'status'
  | 'ping'
  | 'accepted'
  | 'rejected'
  | 'stale'
  | 'reject_pct'
  | 'last_share';

// Coin column. This table is where the answer comes from — the pool a miner
// is pointed at is what decides which chain its hashrate goes to — so it's
// also where a wrong detection is visible and where the user corrects it.
const COIN_LABEL: Record<MinedCoin, string> = {
  btc: 'BTC',
  bch: 'BCH',
};

// How confident the badge should look, and what the tooltip explains.
const COIN_SOURCE_HINT: Record<CoinSource, string> = {
  override: 'Set by you',
  stratum: 'From the network difficulty the miner reports — the pool’s own value',
  address: 'From the payout address format in the pool user',
  pool: 'Guessed from the pool hostname — check this one',
};

const COIN_CHOICES: Array<{ value: MinedCoin | null; label: string }> = [
  { value: null, label: 'Auto' },
  { value: 'btc', label: 'BTC' },
  { value: 'bch', label: 'BCH' },
];

interface SortState {
  key: SortKey;
  dir: 'asc' | 'desc';
}

// Pool list filter. "Active" = the slot the firmware reports as currently
// mining (row.active === true); "Fallback" = every configured backup slot
// (row.slot === 'fallback'); "All" = no filter. A row can be both active
// and fallback at once (a miner that has failed over to its backup pool) —
// in that case it shows under both "Active" and "Fallback".
type PoolFilter = 'all' | 'active' | 'fallback';

const POOL_FILTERS: Array<{ value: PoolFilter; label: string }> = [
  { value: 'active', label: 'Active' },
  { value: 'fallback', label: 'Fallback' },
  { value: 'all', label: 'All' },
];

// Reject% from accepted/rejected. Returns null when neither counter is
// available, or when accepted+rejected == 0 (avoids division-by-zero
// and the meaningless "0/0 = NaN" reject%).
function rejectPct(row: PoolRow): number | null {
  const a = row.accepted ?? 0;
  const r = row.rejected ?? 0;
  if (row.accepted === null && row.rejected === null) return null;
  if (a + r === 0) return null;
  return (r / (a + r)) * 100;
}

// Combined health pill: Alive/Dead/Unknown plus a degraded modifier
// when reject% > 5 (cgminer's own "unhealthy pool" warning threshold).
// AxeOS rows pass `liveOnline` so we can still render a sensible pill
// without the firmware Alive/Dead flag.
function poolHealth(
  row: PoolRow,
): { label: string; tone: 'success' | 'warning' | 'danger' | 'secondary' } {
  const rPct = rejectPct(row);
  const explicit = (row.status ?? '').toLowerCase();
  const aliveExplicit = explicit === 'alive';
  const deadExplicit = explicit === 'dead';

  if (deadExplicit || row.live_online === false) {
    return { label: 'Dead', tone: 'danger' };
  }
  // Degraded if accepting shares but reject% is uncomfortably high.
  // 5% is conservative — cgminer's own log starts warning around 3%
  // but the noise floor for newly-connected miners is often higher.
  if (rPct !== null && rPct >= 5) {
    return { label: `Degraded · ${rPct.toFixed(1)} %`, tone: 'warning' };
  }
  if (aliveExplicit || row.live_online === true) {
    return { label: 'Alive', tone: 'success' };
  }
  return { label: 'Unknown', tone: 'secondary' };
}

// Sort comparator that puts nulls at the bottom for ascending order
// and at the top for descending, so "no data" rows don't claim the
// most-attention top slots.
function compareNullable(
  a: number | string | null,
  b: number | string | null,
  dir: 'asc' | 'desc',
): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === 'number' && typeof b === 'number') {
    return dir === 'asc' ? a - b : b - a;
  }
  return dir === 'asc'
    ? String(a).localeCompare(String(b))
    : String(b).localeCompare(String(a));
}

function fmtCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString();
}

export function PoolsPage() {
  const { data, isLoading, isError, error } = usePools();
  const [sort, setSort] = useState<SortState>({ key: 'miner', dir: 'asc' });
  const [filter, setFilter] = useState<PoolFilter>('active');

  const rows = data?.pools ?? [];

  const sortedRows = useMemo(() => {
    const out = [...rows];
    out.sort((a, b) => {
      let av: number | string | null;
      let bv: number | string | null;
      switch (sort.key) {
        case 'miner':
          av = a.miner_name?.toLowerCase() ?? '';
          bv = b.miner_name?.toLowerCase() ?? '';
          break;
        case 'url':
          av = a.url?.toLowerCase() ?? null;
          bv = b.url?.toLowerCase() ?? null;
          break;
        case 'user':
          av = a.user?.toLowerCase() ?? null;
          bv = b.user?.toLowerCase() ?? null;
          break;
        case 'coin':
          // Null sorts to the bottom via compareNullable, which is what we
          // want: the undetected miners are the ones needing attention and
          // one click sorts them into a block.
          av = a.coin ?? null;
          bv = b.coin ?? null;
          break;
        case 'status':
          av = poolHealth(a).label;
          bv = poolHealth(b).label;
          break;
        case 'ping':
          av = a.ping_ms;
          bv = b.ping_ms;
          break;
        case 'accepted':
          av = a.accepted;
          bv = b.accepted;
          break;
        case 'rejected':
          av = a.rejected;
          bv = b.rejected;
          break;
        case 'stale':
          av = a.stale;
          bv = b.stale;
          break;
        case 'reject_pct':
          av = rejectPct(a);
          bv = rejectPct(b);
          break;
        case 'last_share':
          av = a.last_share_ts;
          bv = b.last_share_ts;
          break;
      }
      return compareNullable(av, bv, sort.dir);
    });
    return out;
  }, [rows, sort]);

  const visibleRows = useMemo(() => {
    switch (filter) {
      case 'active':
        return sortedRows.filter((r) => r.active === true);
      case 'fallback':
        return sortedRows.filter((r) => r.slot === 'fallback');
      default:
        return sortedRows;
    }
  }, [sortedRows, filter]);

  function toggleSort(key: SortKey) {
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
        : { key, dir: 'asc' },
    );
  }

  // Empty state: no miners at all yet. Loading: skeletons.
  if (isLoading) {
    return (
      <div className="space-y-5">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Pools</h1>
          <p className="text-sm text-muted-foreground">
            Stratum endpoints configured on your miners
          </p>
        </header>
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="space-y-5">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Pools</h1>
        </header>
        <Card>
          <CardHeader>
            <CardTitle>Couldn't load pools</CardTitle>
            <CardDescription>
              {error instanceof ApiError ? error.message : 'Network error'}
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    );
  }

  if (!rows.length) {
    return (
      <div className="space-y-5">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Pools</h1>
          <p className="text-sm text-muted-foreground">
            Stratum endpoints configured on your miners
          </p>
        </header>
        <Card>
          <CardHeader>
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-md bg-muted text-muted-foreground">
                <Network className="h-5 w-5" />
              </div>
              <div>
                <CardTitle>No miners yet</CardTitle>
                <CardDescription>
                  Add a miner on the Dashboard and we'll show its pool config
                  here.
                </CardDescription>
              </div>
            </div>
          </CardHeader>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Pools</h1>
          <p className="text-sm text-muted-foreground">
            Stratum endpoints configured on your miners · live
          </p>
        </div>
        {/* Active / Fallback / All segmented filter — same look as the
            range selector on the miner History tab. Purely client-side
            over the already-fetched rows. */}
        <div className="flex gap-1 self-start rounded-lg border border-border bg-card p-1 sm:self-auto">
          {POOL_FILTERS.map((f) => (
            <Button
              key={f.value}
              size="sm"
              variant={filter === f.value ? 'default' : 'ghost'}
              className="h-7 px-3 text-xs"
              onClick={() => setFilter(f.value)}
            >
              {f.label}
            </Button>
          ))}
        </div>
      </header>

      <Card>
        <CardContent className="p-0">
          {/* Horizontal scroll on narrow screens — the table has 9
              columns so it won't fit on a phone. The header sticks at
              the top of the scroll area; rows are clickable to drill
              into the underlying miner. */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-border bg-muted/40 text-xs uppercase tracking-wider text-muted-foreground">
                <tr>
                  <Th label="Miner" sortKey="miner" sort={sort} onSort={toggleSort} />
                  <Th label="URL" sortKey="url" sort={sort} onSort={toggleSort} />
                  <Th label="User" sortKey="user" sort={sort} onSort={toggleSort} />
                  <Th label="Coin" sortKey="coin" sort={sort} onSort={toggleSort} />
                  <Th label="Status" sortKey="status" sort={sort} onSort={toggleSort} />
                  <Th
                    label="Ping"
                    sortKey="ping"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                  <Th
                    label="Accepted"
                    sortKey="accepted"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                  <Th
                    label="Rejected"
                    sortKey="rejected"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                  <Th
                    label="Stale"
                    sortKey="stale"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                  <Th
                    label="Reject %"
                    sortKey="reject_pct"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                  <Th
                    label="Last share"
                    sortKey="last_share"
                    sort={sort}
                    onSort={toggleSort}
                    align="right"
                  />
                </tr>
              </thead>
              <tbody>
                {visibleRows.length === 0 ? (
                  <tr>
                    <td
                      colSpan={11}
                      className="px-3 py-8 text-center text-sm text-muted-foreground"
                    >
                      No pools match the “{filter}” filter.
                    </td>
                  </tr>
                ) : (
                  visibleRows.map((row, idx) => (
                    <PoolRowView key={`${row.miner_id}-${row.slot ?? idx}-${row.url ?? idx}`} row={row} />
                  ))
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        Ping is the round-trip latency to the pool as measured by the miner
        itself — reported by Bitaxe, NerdQAxe/NerdOctaxe (per pool) and Avalon,
        but not by Braiins or LuxOS. Stale and Last share come from
        cgminer-family firmwares (Braiins, LuxOS, Avalon) and aren't exposed by
        AxeOS-based miners. Any value a firmware doesn't report shows "—".
      </p>

      <p className="text-xs text-muted-foreground">
        Coin is which SHA-256 chain a miner's hashrate is going to, used for
        the solo-mining odds on Analytics and for the block-found alert. It's
        detected automatically from the network difficulty the miner reports,
        or from the pool address — click any badge to pin it by hand. Miners
        marked <strong>Not set</strong> are left out of the odds rather than
        credited to the wrong chain; Braiins, LuxOS and Avalon report no
        network difficulty, so those are the ones that may need pinning.
      </p>
    </div>
  );
}

interface ThProps {
  label: string;
  sortKey: SortKey;
  sort: SortState;
  onSort: (key: SortKey) => void;
  align?: 'left' | 'right';
}

function Th({ label, sortKey, sort, onSort, align = 'left' }: ThProps) {
  const active = sort.key === sortKey;
  return (
    <th
      scope="col"
      className={cn(
        'px-3 py-2 font-medium',
        align === 'right' ? 'text-right' : 'text-left',
      )}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 transition-colors hover:text-foreground',
          align === 'right' && 'flex-row-reverse',
          active && 'text-foreground',
        )}
      >
        {label}
        <ArrowUpDown
          className={cn(
            'h-3 w-3 transition-opacity',
            active ? 'opacity-100' : 'opacity-30',
          )}
        />
      </button>
    </th>
  );
}

function PoolRowView({ row }: { row: PoolRow }) {
  const health = poolHealth(row);
  const rPct = rejectPct(row);
  const familyLabel = FAMILY_LABEL[row.family] ?? row.family;

  return (
    <tr className="border-b border-border/60 last:border-0 hover:bg-muted/20">
      <td className="px-3 py-2 align-top">
        <Link
          to={`/miner/${row.miner_id}`}
          className="font-medium text-foreground hover:text-primary hover:underline"
        >
          {row.miner_name}
        </Link>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          <span>{familyLabel}</span>
          {row.active === true && (
            <Badge variant="outline" className="border-primary/40 text-primary">
              active
            </Badge>
          )}
          {row.slot === 'fallback' && row.active !== true && (
            <Badge variant="outline">fallback</Badge>
          )}
        </div>
      </td>
      <td className="px-3 py-2 align-top">
        <span className="break-all font-mono text-xs">{row.url ?? '—'}</span>
      </td>
      <td className="px-3 py-2 align-top">
        <span className="break-all text-xs">{row.user ?? '—'}</span>
      </td>
      <td className="px-3 py-2 align-top">
        <CoinCell row={row} />
      </td>
      <td className="px-3 py-2 align-top">
        <Badge variant={health.tone} className="whitespace-nowrap">
          {health.label}
        </Badge>
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        {row.ping_ms === null ? (
          '—'
        ) : (
          <>
            {row.ping_ms.toFixed(0)} ms
            {row.ping_loss !== null && row.ping_loss > 0 && (
              <div className="text-[11px] text-amber-400">
                {row.ping_loss.toFixed(0)}% loss
              </div>
            )}
          </>
        )}
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        {fmtCount(row.accepted)}
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        {fmtCount(row.rejected)}
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        {fmtCount(row.stale)}
      </td>
      <td className="px-3 py-2 text-right align-top tabular-nums">
        {rPct === null ? '—' : `${rPct.toFixed(2)} %`}
      </td>
      <td className="px-3 py-2 text-right align-top text-muted-foreground">
        {fmtRelative(row.last_share_ts)}
      </td>
    </tr>
  );
}

/**
 * Coin badge with an inline override.
 *
 * Most miners never need touching: firmware that reports its stratum
 * network difficulty (the Bitaxe family) is detected automatically, and so
 * are pools whose hostname or payout address names the chain. The picker
 * exists for the rest — Braiins, LuxOS and Canaan on a pool that gives
 * nothing away — where the estimates on the Analytics tab and the
 * block-found alert would otherwise have nothing to go on.
 */
function CoinCell({ row }: { row: PoolRow }) {
  const [editing, setEditing] = useState(false);
  const setCoin = useSetMinerCoin(row.miner_id);
  const pinned = row.coin_override !== null;

  if (editing) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex gap-1 rounded-lg border border-border bg-card p-1">
          {COIN_CHOICES.map((choice) => (
            <Button
              key={choice.label}
              size="sm"
              variant={row.coin_override === choice.value ? 'default' : 'ghost'}
              className="h-6 px-2 text-[11px]"
              disabled={setCoin.isPending}
              onClick={() => {
                setCoin.mutate(choice.value, { onSuccess: () => setEditing(false) });
              }}
            >
              {choice.label}
            </Button>
          ))}
        </div>
        <button
          type="button"
          className="text-left text-[10px] text-muted-foreground hover:text-foreground"
          onClick={() => setEditing(false)}
        >
          Cancel
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setEditing(true)}
      title={
        row.coin_source
          ? COIN_SOURCE_HINT[row.coin_source]
          : 'We couldn’t detect this miner’s coin — click to set it'
      }
      className="flex flex-col items-start gap-0.5 text-left"
    >
      {row.coin ? (
        <Badge
          variant={pinned ? 'outline' : 'secondary'}
          className="whitespace-nowrap"
        >
          {COIN_LABEL[row.coin]}
        </Badge>
      ) : (
        <Badge variant="warning" className="whitespace-nowrap">
          Not set
        </Badge>
      )}
      <span className="text-[10px] text-muted-foreground">
        {pinned ? 'pinned' : row.coin ? 'detected' : 'click to set'}
      </span>
    </button>
  );
}
