import { useMemo, useState } from 'react';
import { Heart, Loader2, Zap } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { DonateBtcCard } from '@/components/DonateBtcCard';
import { FAMILY_LABEL, fmtNum } from '@/lib/format';
import { familySupportsDonation, fmtRemaining } from '@/lib/donation';
import { cn } from '@/lib/utils';
import {
  useDonationInfo,
  useDonations,
  useMiners,
  useStartDonation,
  useStopDonation,
  useStopDonationMiner,
} from '@/api/hooks';
import type { DonationMinerRow, StartDonationResponse } from '@/lib/types';

// Full Donations page (replaces the old DonateDialog modal). Four
// sections, top to bottom:
//   1. Support MinerWatch (BTC) — the classic address + QR (DonateBtcCard)
//   2. How it works — always-visible explainer (lottery + real cost)
//   3. Start a donation — pick miners + hours, then Donate
//   4. Active donations — live table with per-row STOP
// See docs/donate-hashrate-design.md.
//
// Sections 2 and 3 are currently hidden — see SHOW_HASHRATE_DONATION.

// Hashrate donation is hidden for now: donating hashrate is a solo-mining
// lottery that usually pays the project nothing, so the page asks for
// Bitcoin instead, which is what actually keeps development going.
//
// Kept behind a flag rather than deleted, in the same spirit as the
// retained-but-unmounted DonateDialog: flip this to `true` to bring both
// sections back, no rebuilding required. The backend endpoints and the
// donation controller are untouched, so donations already in flight keep
// running and keep reverting on schedule.
const SHOW_HASHRATE_DONATION = false;

export function DonationsPage() {
  const { data: info } = useDonationInfo();
  const { data: minersResp } = useMiners();
  const { data: donationsResp } = useDonations();

  const start = useStartDonation();
  const stopMiner = useStopDonationMiner();
  const stopDonation = useStopDonation();

  const miners = minersResp?.miners ?? [];
  const activeRows = donationsResp?.donations ?? [];

  // Miner ids currently in flight — can't be re-donated, shown as such.
  const busyIds = useMemo(
    () => new Set(activeRows.map((r) => r.miner_id)),
    [activeRows],
  );

  const minHours = info?.min_hours ?? 0.1;
  const maxHours = info?.max_hours ?? 72;
  const defaultHours = info?.default_hours ?? 6;

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [hours, setHours] = useState<number>(defaultHours);
  const [result, setResult] = useState<StartDonationResponse | null>(null);

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const canDonate = selected.size > 0 && hours >= minHours && hours <= maxHours;

  const endTimeLabel = useMemo(() => {
    const end = new Date(Date.now() + hours * 3600 * 1000);
    return end.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }, [hours]);

  async function onDonate() {
    if (!canDonate) return;
    const res = await start.mutateAsync({
      miner_ids: [...selected],
      hours,
    });
    setResult(res);
    // Clear the ones that actually started so they don't linger selected.
    const started = new Set(
      res.miners.filter((m) => m.status === 'active').map((m) => m.miner_id),
    );
    setSelected((prev) => new Set([...prev].filter((id) => !started.has(id))));
  }

  async function onStopAll() {
    const ids = new Set(activeRows.map((r) => r.donation_id));
    await Promise.all([...ids].map((id) => stopDonation.mutateAsync(id)));
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Heart className="h-6 w-6 text-red-500" />
          Donations
        </h1>
        <p className="text-sm text-muted-foreground">
          Support MinerWatch with Bitcoin.
        </p>
      </div>

      <div className={cn('grid gap-6', SHOW_HASHRATE_DONATION && 'lg:grid-cols-2')}>
        {/* 1. BTC donation */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Donate Bitcoin</CardTitle>
            <CardDescription>
              Send any amount to the project address — Bitcoin on-chain or Lightning.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <DonateBtcCard />
          </CardContent>
        </Card>

        {/* 2. How it works */}
        {SHOW_HASHRATE_DONATION && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Zap className="h-4 w-4 text-primary" />
              Donate hashrate
            </CardTitle>
            <CardDescription>How it works — please read</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-muted-foreground">
            <p>
              Lend MinerWatch some of your mining power instead of sending BTC.
              Pick one or more miners and how long, and they&apos;ll mine to
              MinerWatch&apos;s Bitcoin address on{' '}
              <span className="font-mono text-foreground">
                {info?.pool_url ?? 'solo.ckpool.org'}
              </span>{' '}
              for that time — then switch back automatically.
            </p>
            <ul className="space-y-2">
              <li>
                <span className="font-medium text-foreground">It&apos;s a lottery, not a transfer.</span>{' '}
                Solo mining pays the whole block reward to whoever finds a block,
                or nothing. Over a few hours a home miner&apos;s odds are small —
                you&apos;re contributing hashrate and a shot at the jackpot, not a
                fixed amount.
              </li>
              <li>
                <span className="font-medium text-foreground">Everyone shares one address</span>,
                so all donated hashrate competes for blocks together — the more
                people donating at once, the better the collective odds.
              </li>
              <li>
                <span className="font-medium text-foreground">It costs you real electricity</span>{' '}
                and the pool revenue you&apos;d have earned for that time. It&apos;s a
                genuine donation.
              </li>
              <li>
                <span className="font-medium text-foreground">It reverts automatically</span>{' '}
                when the timer ends. You can STOP any miner early, and active
                donations stay visible below even after a restart.
              </li>
            </ul>
            <p className="text-[11px]">
              Only AxeOS miners (Bitaxe, NerdQAxe) support this today. Other
              families show up disabled below.
            </p>
          </CardContent>
        </Card>
        )}
      </div>

      {/* 3. Start a donation */}
      {SHOW_HASHRATE_DONATION && (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Start a donation</CardTitle>
          <CardDescription>
            Choose which miners to lend and for how long.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {miners.length === 0 ? (
            <p className="text-sm text-muted-foreground">No miners found yet.</p>
          ) : (
            <div className="space-y-1.5">
              {miners.map((m) => {
                const supported = familySupportsDonation(m.family);
                const busy = busyIds.has(m.id);
                const disabled = !supported || busy;
                const hr = m.last_metric?.hashrate_ths ?? null;
                return (
                  <label
                    key={m.id}
                    className={cn(
                      'flex items-center gap-3 rounded-md border border-border px-3 py-2 text-sm',
                      disabled
                        ? 'cursor-not-allowed opacity-60'
                        : 'cursor-pointer hover:bg-accent',
                    )}
                  >
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-primary"
                      disabled={disabled}
                      checked={selected.has(m.id)}
                      onChange={() => toggle(m.id)}
                    />
                    <div className="flex min-w-0 flex-1 flex-col">
                      <span className="font-medium text-foreground">{m.name}</span>
                      {!supported && (
                        <span className="text-[11px] text-muted-foreground">
                          If MinerWatch is useful to you, you can manually set{' '}
                          <span className="font-mono break-all text-foreground">
                            bc1qexhamvrpclpr2skyyw3u8edm8kznnvt6zjudxu.donations
                          </span>{' '}
                          and{' '}
                          <span className="font-mono break-all text-foreground">
                            stratum+tcp://solo.ckpool.org:3333
                          </span>{' '}
                          to donate a bit of hashing power.
                        </span>
                      )}
                      <span className="text-[11px] text-muted-foreground">
                        {FAMILY_LABEL[m.family] ?? m.family}
                        {hr !== null ? ` · ${fmtNum(hr, 2, ' TH/s')}` : ''}
                      </span>
                    </div>
                    {busy ? (
                      <Badge variant="success">Donating</Badge>
                    ) : !supported ? (
                      <Badge variant="outline">Not supported yet</Badge>
                    ) : null}
                  </label>
                );
              })}
            </div>
          )}

          <div className="flex flex-wrap items-end gap-4">
            <div className="space-y-1">
              <label className="text-xs uppercase tracking-wider text-muted-foreground">
                Duration (hours)
              </label>
              <input
                type="number"
                min={minHours}
                max={maxHours}
                step={1}
                value={hours}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  if (Number.isFinite(v)) {
                    setHours(Math.max(minHours, Math.min(maxHours, v)));
                  }
                }}
                className="h-9 w-28 rounded-md border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <Button onClick={onDonate} disabled={!canDonate || start.isPending}>
              {start.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Starting…
                </>
              ) : (
                <>
                  <Zap className="h-4 w-4" />
                  Donate
                </>
              )}
            </Button>
          </div>

          {selected.size > 0 && (
            <p className="text-xs text-muted-foreground">
              You&apos;re about to donate{' '}
              <span className="font-medium text-foreground">
                {selected.size} miner{selected.size > 1 ? 's' : ''}
              </span>{' '}
              for <span className="font-medium text-foreground">{hours} h</span>.
              They&apos;ll mine to MinerWatch&apos;s address (solo lottery) and pay
              you nothing during this time. They&apos;ll switch back automatically
              around {endTimeLabel}.
            </p>
          )}

          {start.isError && (
            <p className="text-xs text-red-400">
              Couldn&apos;t start the donation. Please try again.
            </p>
          )}
          {result && <StartResultSummary result={result} />}
        </CardContent>
      </Card>
      )}

      {/* 4. Active donations — still shown while any donation is in flight,
          even with the sections above hidden, so nobody is left mining to
          the project address with no way to see it or stop it early. Once
          the last one reverts, the card disappears with them. */}
      {(SHOW_HASHRATE_DONATION || activeRows.length > 0) && (
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0">
          <div>
            <CardTitle className="text-base">Active donations</CardTitle>
            <CardDescription>
              Live status of every miner you&apos;re donating right now.
            </CardDescription>
          </div>
          {activeRows.length > 0 && (
            <Button
              variant="outline"
              size="sm"
              onClick={onStopAll}
              disabled={stopDonation.isPending}
            >
              Stop all
            </Button>
          )}
        </CardHeader>
        <CardContent>
          {activeRows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No active donations. When you donate hashrate, your miners show up
              here with a live status and a STOP button.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b border-border bg-muted/40 text-xs uppercase tracking-wider text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">Miner</th>
                    <th className="px-3 py-2 text-right">Hashrate</th>
                    <th className="px-3 py-2 text-right">Time left</th>
                    <th className="px-3 py-2 text-left">Status</th>
                    <th className="px-3 py-2 text-right" />
                  </tr>
                </thead>
                <tbody>
                  {activeRows.map((row) => (
                    <DonationRow
                      key={row.id}
                      row={row}
                      onStop={() =>
                        stopMiner.mutate({
                          donationId: row.donation_id,
                          dmId: row.id,
                        })
                      }
                      stopping={stopMiner.isPending}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      )}
    </div>
  );
}

function StartResultSummary({ result }: { result: StartDonationResponse }) {
  const started = result.miners.filter((m) => m.status === 'active').length;
  const unsupported = result.miners.filter((m) => m.status === 'unsupported');
  const errored = result.miners.filter((m) => m.status === 'error');
  return (
    <div className="space-y-1 text-xs">
      {started > 0 && (
        <p className="text-emerald-400">
          Started {started} miner{started > 1 ? 's' : ''}.
        </p>
      )}
      {unsupported.map((m) => (
        <p key={`u-${m.miner_id}`} className="text-muted-foreground">
          Miner #{m.miner_id}: {m.error ?? 'not supported'}
        </p>
      ))}
      {errored.map((m) => (
        <p key={`e-${m.miner_id}`} className="text-red-400">
          Miner #{m.miner_id}: {m.error ?? 'failed'}
        </p>
      ))}
    </div>
  );
}

function statusBadge(row: DonationMinerRow) {
  if (row.status === 'unreachable') {
    return { label: 'Reverting…', variant: 'warning' as const };
  }
  if (!row.online) {
    return { label: 'Switching…', variant: 'warning' as const };
  }
  if (row.confirmed) {
    return { label: 'Donating', variant: 'success' as const };
  }
  return { label: 'Switching…', variant: 'warning' as const };
}

function DonationRow({
  row,
  onStop,
  stopping,
}: {
  row: DonationMinerRow;
  onStop: () => void;
  stopping: boolean;
}) {
  const badge = statusBadge(row);
  return (
    <tr className="border-b border-border/60 last:border-0">
      <td className="px-3 py-2">
        <div className="font-medium text-foreground">{row.miner_name ?? `#${row.miner_id}`}</div>
        <div className="text-[11px] text-muted-foreground">
          {row.family ? FAMILY_LABEL[row.family] ?? row.family : row.host}
        </div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {row.hashrate_ths !== null ? fmtNum(row.hashrate_ths, 2, ' TH/s') : '—'}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">
        {fmtRemaining(row.seconds_remaining)}
      </td>
      <td className="px-3 py-2">
        <Badge variant={badge.variant}>{badge.label}</Badge>
        {row.last_error && row.status === 'unreachable' && (
          <div className="mt-0.5 text-[10px] text-muted-foreground">retrying…</div>
        )}
      </td>
      <td className="px-3 py-2 text-right">
        <Button variant="destructive" size="sm" onClick={onStop} disabled={stopping}>
          STOP
        </Button>
      </td>
    </tr>
  );
}
