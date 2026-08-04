import { useEffect, useState } from 'react';
import { Award, HelpCircle, Target } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { fmtDifficulty, fmtEta, fmtNum, fmtProb } from '@/lib/format';
import type {
  PredictionCoin,
  PredictionGroup,
  PredictionResponse,
  PredictionWindow,
} from '@/lib/types';

interface Props {
  data: PredictionResponse | null;
  // Mode of the "Find a block (solo)" block, plus its setter so the in-card
  // toggle can switch between the real per-coin odds and the BTC/BCH what-if.
  coin?: PredictionCoin;
  onCoinChange?: (coin: PredictionCoin) => void;
}

const COIN_OPTIONS: Array<{ value: PredictionCoin; label: string }> = [
  { value: 'auto', label: 'Auto' },
  { value: 'btc', label: 'BTC' },
  { value: 'bch', label: 'BCH' },
];

const COIN_LABEL: Record<PredictionCoin, string> = {
  auto: 'Mined coin',
  btc: 'Bitcoin (BTC)',
  bch: 'Bitcoin Cash (BCH)',
};

function CoinToggle({
  coin,
  onChange,
}: {
  coin: PredictionCoin;
  onChange: (coin: PredictionCoin) => void;
}) {
  return (
    <div className="flex gap-1 rounded-lg border border-border bg-card p-1">
      {COIN_OPTIONS.map((o) => (
        <Button
          key={o.value}
          size="sm"
          variant={coin === o.value ? 'default' : 'ghost'}
          className="h-6 px-2 text-[11px]"
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

/**
 * Predictions widget: probabilities computed server-side at
 * /api/fleet/prediction using the Poisson model
 * P(t) = 1 - exp(-rate · t) where rate = H / (D · 2^32).
 *
 * The "Find a block (solo)" block has two modes:
 *
 *   * Auto — the real odds. The backend splits the fleet by the coin each
 *     miner is actually on and returns one group per coin, each pairing
 *     that coin's OWN hashrate with that coin's OWN difficulty. A fleet
 *     mining both BTC and BCH therefore gets a sub-tab each, rather than
 *     one blended number that belongs to neither.
 *   * BTC / BCH — a what-if: the whole fleet's hashrate against that coin's
 *     difficulty, labelled as such so it can't be mistaken for the real odds.
 *
 * Renders nothing until both fleet hashrate and an all-time best are
 * available (the backend already returns null in that case, but we
 * also gate at the card level to avoid showing an empty header).
 */
export function PredictionsCard({ data, coin = 'auto', onCoinChange }: Props) {
  const groups = data?.groups ?? [];
  // Sub-tab selection lives here rather than in the page: it's a view
  // preference over one payload, not app state. Keyed by coin id, with
  // '__unknown' standing in for the unclassified group (which has a null
  // coin and so can't key a string state directly).
  const [activeGroup, setActiveGroup] = useState<string | null>(null);

  // Keep the selection valid as the fleet changes underneath us — a miner
  // going offline can make a whole group disappear between polls.
  const groupKeys = groups.map(groupKey).join('|');
  useEffect(() => {
    const keys = groupKeys ? groupKeys.split('|') : [];
    setActiveGroup((current) =>
      current && keys.includes(current) ? current : (keys[0] ?? null),
    );
  }, [groupKeys]);

  if (!data) return null;
  const hasFleetHash = data.fleet_hashrate_ths && data.fleet_hashrate_ths > 0;
  const hasBest = !!(data.best_alltime && data.best_alltime.value);
  if (!hasFleetHash || !hasBest) return null;

  const beatBest = data.predictions.beat_best;
  const findBlock = data.predictions.find_block;

  // Sub-tabs are the honest view, so they lead whenever the backend gave us
  // groups and we're not in an explicit what-if. A single-group fleet needs
  // no tab strip — one coin, one estimate, same as it always looked.
  const showGroups = coin === 'auto' && groups.length > 0;
  const selected =
    groups.find((g) => groupKey(g) === activeGroup) ?? groups[0] ?? null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div>
          <CardTitle className="text-base">Predictions</CardTitle>
          <p className="text-xs text-muted-foreground">
            Statistical odds based on current fleet hashrate
          </p>
        </div>
        <div className="text-right text-xs tabular-nums text-muted-foreground leading-relaxed">
          <div>{fmtNum(data.fleet_hashrate_ths, 2)} TH/s fleet</div>
          <div>best {fmtDifficulty(data.best_alltime!.value)}</div>
          {!showGroups && data.network_difficulty && (
            <div>net {fmtDifficulty(data.network_difficulty)}</div>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <PredictionBlock
            icon={Award}
            iconTone="text-chart-performance"
            title="Beat all-time best"
            subtitle={`Current record: ${fmtDifficulty(data.best_alltime!.value)}`}
            window={beatBest}
            footnote="Any miner on any SHA-256 chain can set the record, so this uses the whole fleet."
          />
          {showGroups ? (
            <GroupedFindBlock
              groups={groups}
              selected={selected}
              activeKey={activeGroup}
              onSelect={setActiveGroup}
              headerExtra={
                onCoinChange ? <CoinToggle coin={coin} onChange={onCoinChange} /> : undefined
              }
            />
          ) : (
            <PredictionBlock
              icon={Target}
              iconTone="text-chart-mining"
              title="Find a block (solo)"
              subtitle={whatIfSubtitle(coin, data.network_difficulty)}
              window={findBlock}
              emptyMessage={emptyMessage(coin, data.network_difficulty)}
              footnote={
                coin === 'auto'
                  ? undefined
                  : `What-if: your entire ${fmtNum(data.fleet_hashrate_ths, 2)} TH/s pointed at ${COIN_LABEL[coin]}.`
              }
              headerExtra={
                onCoinChange ? <CoinToggle coin={coin} onChange={onCoinChange} /> : undefined
              }
            />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// Stable key for a group, including the null-coin "unclassified" bucket.
function groupKey(group: PredictionGroup): string {
  return group.coin ?? '__unknown';
}

function whatIfSubtitle(coin: PredictionCoin, difficulty: number | null): string {
  if (difficulty) {
    return coin === 'auto'
      ? `Network difficulty: ${fmtDifficulty(difficulty)}`
      : `${COIN_LABEL[coin]} · difficulty ${fmtDifficulty(difficulty)}`;
  }
  return coin === 'auto'
    ? 'No network difficulty reported by your miners yet'
    : COIN_LABEL[coin];
}

function emptyMessage(
  coin: PredictionCoin,
  difficulty: number | null,
): string | undefined {
  if (difficulty) return undefined;
  return coin === 'auto'
    ? 'Your miners haven’t reported a network difficulty yet — pick BTC or BCH to estimate the odds.'
    : `Couldn't load ${COIN_LABEL[coin]} network difficulty right now — try again shortly.`;
}

/**
 * "Find a block" with one sub-tab per coin the fleet is actually mining.
 *
 * Each tab's numbers come only from that coin's miners, which is the whole
 * point: 5 TH/s on BTC and 0.5 TH/s on BCH are two separate races, and
 * showing 5.5 TH/s against either difficulty would be wrong for both.
 */
function GroupedFindBlock({
  groups,
  selected,
  activeKey,
  onSelect,
  headerExtra,
}: {
  groups: PredictionGroup[];
  selected: PredictionGroup | null;
  activeKey: string | null;
  onSelect: (key: string) => void;
  headerExtra?: React.ReactNode;
}) {
  const multi = groups.length > 1;
  const group = selected;

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/30 p-4">
      <header className="flex items-center gap-3">
        <div className="flex h-8 w-8 items-center justify-center rounded-md bg-card-foreground/5 text-chart-mining">
          <Target className="h-4 w-4" />
        </div>
        <div>
          <div className="text-sm font-semibold uppercase tracking-wider">
            Find a block (solo)
          </div>
          <div className="text-xs text-muted-foreground">
            {group ? groupSubtitle(group) : 'No miners online'}
          </div>
        </div>
        {headerExtra && <div className="ml-auto">{headerExtra}</div>}
      </header>

      {multi && (
        <div className="flex flex-wrap gap-1 rounded-lg border border-border bg-card p-1">
          {groups.map((g) => {
            const key = groupKey(g);
            const active = key === (activeKey ?? groupKey(groups[0]));
            return (
              <Button
                key={key}
                size="sm"
                variant={active ? 'default' : 'ghost'}
                className="h-6 gap-1.5 px-2 text-[11px]"
                onClick={() => onSelect(key)}
              >
                {g.coin === null && <HelpCircle className="h-3 w-3" />}
                {g.ticker ?? 'Unknown'}
                <span className="tabular-nums opacity-70">
                  {fmtNum(g.hashrate_ths, 2)} TH/s
                </span>
              </Button>
            );
          })}
        </div>
      )}

      {!group ? (
        <p className="text-xs text-muted-foreground">
          Not enough data yet — waiting for live hashrate and a known target.
        </p>
      ) : group.coin === null ? (
        <UnknownGroupNotice group={group} />
      ) : !group.find_block ? (
        <p className="text-xs text-muted-foreground">
          Couldn't determine the {group.label} network difficulty right now — try
          again shortly.
        </p>
      ) : (
        <>
          <ExpectedTime window={group.find_block} />
          <ProbBars window={group.find_block} />
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            Based on the {fmtNum(group.hashrate_ths, 2)} TH/s from{' '}
            {group.miner_count} {group.miner_count === 1 ? 'miner' : 'miners'} on{' '}
            {group.label}
            {group.difficulty_source === 'explorer' &&
              ' · difficulty from a public explorer (your miners don’t report one)'}
            .
          </p>
        </>
      )}
    </div>
  );
}

function groupSubtitle(group: PredictionGroup): string {
  if (group.coin === null) {
    return `${group.miner_count} ${group.miner_count === 1 ? 'miner' : 'miners'} — coin not detected`;
  }
  const difficulty = group.network_difficulty
    ? ` · difficulty ${fmtDifficulty(group.network_difficulty)}`
    : '';
  return `${group.label}${difficulty}`;
}

/**
 * The unclassified group. Deliberately shows no odds: guessing a coin for
 * this hashrate would silently skew whichever estimate it was folded into.
 * It's a to-do, so it points at where to fix it.
 */
function UnknownGroupNotice({ group }: { group: PredictionGroup }) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-card px-3 py-3">
      <div className="flex items-center gap-2">
        <Badge variant="secondary">Not counted</Badge>
        <span className="text-xs tabular-nums text-muted-foreground">
          {fmtNum(group.hashrate_ths, 2)} TH/s
        </span>
      </div>
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        We couldn't tell which coin{' '}
        {group.miner_count === 1 ? 'this miner is' : 'these miners are'} mining,
        so their hashrate is left out of the estimates above rather than
        credited to the wrong chain. Set it on the{' '}
        <a className="text-primary hover:underline" href="/pools">
          Pools page
        </a>{' '}
        and it'll be included from the next refresh.
      </p>
    </div>
  );
}

function ExpectedTime({ window: w }: { window: PredictionWindow }) {
  return (
    <div className="flex items-baseline justify-between rounded-md border border-border bg-card px-3 py-2">
      <span className="text-[11px] uppercase tracking-wider text-muted-foreground">
        Expected time
      </span>
      <span className="text-lg font-bold tabular-nums text-primary">
        {fmtEta(w.expected_time_s)}
      </span>
    </div>
  );
}

function ProbBars({ window: w }: { window: PredictionWindow }) {
  return (
    <div className="flex flex-col gap-2">
      <ProbBar label="Within 1 hour" p={w.probability['1h']} />
      <ProbBar label="Within 24 hours" p={w.probability['24h']} />
      <ProbBar label="Within 7 days" p={w.probability['7d']} />
    </div>
  );
}

interface BlockProps {
  icon: React.ComponentType<{ className?: string }>;
  iconTone: string;
  title: string;
  subtitle: string;
  window: PredictionWindow | null;
  // Optional node rendered at the right edge of the header (e.g. the
  // BTC/BCH coin toggle on the "Find a block" block).
  headerExtra?: React.ReactNode;
  // Optional override for the text shown when there's no prediction window.
  emptyMessage?: string;
  // Optional small print under the bars, e.g. flagging a what-if.
  footnote?: string;
}

function PredictionBlock({
  icon: Icon,
  iconTone,
  title,
  subtitle,
  window: w,
  headerExtra,
  emptyMessage,
  footnote,
}: BlockProps) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-muted/30 p-4">
      <header className="flex items-center gap-3">
        <div className={`flex h-8 w-8 items-center justify-center rounded-md bg-card-foreground/5 ${iconTone}`}>
          <Icon className="h-4 w-4" />
        </div>
        <div>
          <div className="text-sm font-semibold uppercase tracking-wider">{title}</div>
          <div className="text-xs text-muted-foreground">{subtitle}</div>
        </div>
        {headerExtra && <div className="ml-auto">{headerExtra}</div>}
      </header>

      {!w ? (
        <p className="text-xs text-muted-foreground">
          {emptyMessage ?? 'Not enough data yet — waiting for live hashrate and a known target.'}
        </p>
      ) : (
        <>
          <ExpectedTime window={w} />
          <ProbBars window={w} />
          {footnote && (
            <p className="text-[11px] leading-relaxed text-muted-foreground">{footnote}</p>
          )}
        </>
      )}
    </div>
  );
}

function ProbBar({ label, p }: { label: string; p: number | null | undefined }) {
  const pct = (p === null || p === undefined || !Number.isFinite(p))
    ? 0
    : Math.max(0, Math.min(1, p)) * 100;
  return (
    <div className="grid grid-cols-[110px_1fr_60px] items-center gap-2 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="relative block h-2 rounded-full border border-border bg-card overflow-hidden">
        <span
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-primary/80 to-primary transition-all"
          style={{ width: `${pct.toFixed(2)}%` }}
        />
      </span>
      <span className="text-right tabular-nums">{fmtProb(p)}</span>
    </div>
  );
}
