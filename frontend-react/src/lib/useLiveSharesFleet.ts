import { useEffect, useRef, useState } from 'react';

import type { LiveShareEvent, LiveSharesStats } from '@/lib/types';

// Fleet-wide live shares: subscribes to the per-miner SSE streams in
// parallel and exposes the combined state as a single React value.
//
// Why one EventSource per miner instead of a new aggregated backend
// endpoint? The per-miner stream already exists, is battle-tested,
// and the home-fleet scale (a handful of AxeOS devices) doesn't
// stress the browser's per-origin connection limit. If we later
// outgrow that, we can replace this hook with a single subscription
// to a future ``/api/fleet/shares/stream`` without changing call
// sites.
//
// Memory model: we keep one ring buffer of ``LiveShareEventWithMiner``
// per miner (capped at MAX_PER_MINER) so a single noisy device can't
// starve the others. The chart further windows the events by time.
//
// Reconnect model: EventSource auto-reconnects on transient errors.
// We mirror that into a per-miner ``connected`` boolean so the UI
// can show a "Connecting…" badge when something is down. The hook
// itself doesn't try to be clever about backoff — that's the
// browser's job.

// Retention is TIME-based, not count-based. A pure per-miner count cap
// evicts a high-throughput miner's older events within a few minutes:
// a multi-ASIC SupraHex (6× BM1368, ~5-6 TH/s) logs results several
// times faster than a single-ASIC Gamma, so a 1000-event cap covered
// only ~5 min for the Hex while the Gamma's 1000 events still spanned
// the whole 10-min window — the Hex "dropped off" the left of the chart.
// Keeping the last RETENTION_MS of events instead gives every miner the
// same time span regardless of rate. HARD_CAP is only a memory backstop
// for a pathological result rate.
const RETENTION_MS = 15 * 60 * 1000; // ≥ the longest selectable range (10m) + margin
const HARD_CAP = 20000;

// Drop events older than the retention window, then clamp to HARD_CAP.
// Events are appended in arrival order (ts ascending), so expired ones
// are always at the front — the while-loop is amortised O(1) per push.
function pruneByTime<T extends { ts: number }>(events: T[], now: number): T[] {
  const cutoff = now - RETENTION_MS;
  let start = 0;
  while (start < events.length && events[start].ts < cutoff) start += 1;
  let out = start > 0 ? events.slice(start) : events;
  if (out.length > HARD_CAP) out = out.slice(out.length - HARD_CAP);
  return out;
}

export interface LiveShareEventWithMiner extends LiveShareEvent {
  minerId: number;
}

export interface LiveSharesFleetState {
  /** Per-miner ring of events (most recent last), keyed by miner id. */
  eventsByMiner: Record<number, LiveShareEventWithMiner[]>;
  /** Per-miner connection status (true = SSE open). */
  connectedByMiner: Record<number, boolean>;
  /** Per-miner cumulative stats as reported by the backend. */
  statsByMiner: Record<number, LiveSharesStats | null>;
}

function toMs(e: LiveShareEvent): LiveShareEvent {
  return { ...e, ts: e.ts * 1000 };
}

/**
 * Subscribe to live share events for a list of miners. Order of
 * ``minerIds`` is not significant; the hook keys everything by id.
 * Passing an empty array tears down all subscriptions.
 */
export function useLiveSharesFleet(minerIds: number[]): LiveSharesFleetState {
  const [state, setState] = useState<LiveSharesFleetState>({
    eventsByMiner: {},
    connectedByMiner: {},
    statsByMiner: {},
  });

  // Stable membership key. EventSource lifecycle is keyed off the
  // *set* of ids, not their order, so we sort+join into a string.
  const membership = [...minerIds].sort((a, b) => a - b).join(',');

  // Keep a ref to the latest EventSources so the cleanup function in
  // the effect can close them on rebind.
  const sourcesRef = useRef<Record<number, EventSource>>({});

  useEffect(() => {
    const ids = membership ? membership.split(',').map((s) => Number(s)) : [];

    // Tear down sources we no longer need.
    for (const [idStr, es] of Object.entries(sourcesRef.current)) {
      const id = Number(idStr);
      if (!ids.includes(id)) {
        es.close();
        delete sourcesRef.current[id];
      }
    }

    // Clear state for miners that left the set (so stale points
    // don't linger in the chart when the user toggles a miner off
    // and on again later).
    setState((prev) => {
      const nextEvents = { ...prev.eventsByMiner };
      const nextConn = { ...prev.connectedByMiner };
      const nextStats = { ...prev.statsByMiner };
      for (const key of Object.keys(nextEvents)) {
        const id = Number(key);
        if (!ids.includes(id)) {
          delete nextEvents[id];
          delete nextConn[id];
          delete nextStats[id];
        }
      }
      return { eventsByMiner: nextEvents, connectedByMiner: nextConn, statsByMiner: nextStats };
    });

    // Open sources for newly-added miners.
    for (const id of ids) {
      if (sourcesRef.current[id]) continue;
      const es = new EventSource(`/api/miners/${id}/shares/stream`);
      sourcesRef.current[id] = es;

      es.addEventListener('snapshot', (ev) => {
        try {
          const payload = JSON.parse((ev as MessageEvent).data);
          const evs: LiveShareEventWithMiner[] = (payload.events ?? [])
            .map(toMs)
            .map((e: LiveShareEvent) => ({ ...e, minerId: id }));
          setState((prev) => ({
            ...prev,
            eventsByMiner: { ...prev.eventsByMiner, [id]: pruneByTime(evs, Date.now()) },
            statsByMiner: { ...prev.statsByMiner, [id]: payload.stats ?? null },
            connectedByMiner: { ...prev.connectedByMiner, [id]: true },
          }));
        } catch {
          /* ignore malformed frame */
        }
      });

      es.addEventListener('share', (ev) => {
        try {
          const raw = toMs(JSON.parse((ev as MessageEvent).data));
          const e: LiveShareEventWithMiner = { ...raw, minerId: id };
          setState((prev) => {
            const cur = prev.eventsByMiner[id] ?? [];
            const next = cur.slice();
            next.push(e);
            const pruned = pruneByTime(next, Date.now());
            const curStats = prev.statsByMiner[id] ?? null;
            const nextStats = curStats
              ? {
                  ...curStats,
                  results_total: curStats.results_total + 1,
                  submitted_total: curStats.submitted_total + (e.submitted ? 1 : 0),
                  current_target: e.target,
                  last_event_ts: e.ts / 1000,
                }
              : curStats;
            return {
              ...prev,
              eventsByMiner: { ...prev.eventsByMiner, [id]: pruned },
              statsByMiner: { ...prev.statsByMiner, [id]: nextStats },
            };
          });
        } catch {
          /* ignore */
        }
      });

      es.addEventListener('verdict', (ev) => {
        try {
          const v = JSON.parse((ev as MessageEvent).data) as { seq: number; accepted: boolean };
          setState((prev) => {
            const cur = prev.eventsByMiner[id] ?? [];
            const next = cur.map((e) => (e.seq === v.seq ? { ...e, accepted: v.accepted } : e));
            const curStats = prev.statsByMiner[id] ?? null;
            const nextStats = curStats
              ? {
                  ...curStats,
                  accepted_total: curStats.accepted_total + (v.accepted ? 1 : 0),
                  rejected_total: curStats.rejected_total + (v.accepted ? 0 : 1),
                }
              : curStats;
            return {
              ...prev,
              eventsByMiner: { ...prev.eventsByMiner, [id]: next },
              statsByMiner: { ...prev.statsByMiner, [id]: nextStats },
            };
          });
        } catch {
          /* ignore */
        }
      });

      es.onopen = () =>
        setState((prev) => ({
          ...prev,
          connectedByMiner: { ...prev.connectedByMiner, [id]: true },
        }));
      es.onerror = () =>
        setState((prev) => ({
          ...prev,
          connectedByMiner: { ...prev.connectedByMiner, [id]: false },
        }));
    }

    return () => {
      // Component unmount or membership change: the loops above will
      // close stale sources next time, but on unmount the effect's
      // cleanup runs without re-entering the body, so we close
      // everything here.
      for (const es of Object.values(sourcesRef.current)) {
        es.close();
      }
      sourcesRef.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [membership]);

  return state;
}

/**
 * Deterministic color for a miner id. The palette is hand-picked to
 * stay distinguishable on both dark and light themes; the modulo keeps
 * the mapping stable across reloads even when miner ids are sparse.
 */
const MINER_COLOR_PALETTE = [
  '#5db0ff', // primary blue
  '#22c55e', // emerald
  '#f97316', // orange
  '#a855f7', // purple
  '#eab308', // amber
  '#06b6d4', // cyan
  '#ec4899', // pink
  '#84cc16', // lime
  '#f43f5e', // rose
  '#14b8a6', // teal
  '#8b5cf6', // violet
  '#f59e0b', // gold
];

export function colorForMinerId(id: number): string {
  // Mix the bits a little so consecutive ids don't always pick
  // consecutive palette slots; otherwise users with ids 1..N would
  // see a rainbow gradient regardless of name.
  const hashed = ((id * 2654435761) >>> 0) % MINER_COLOR_PALETTE.length;
  return MINER_COLOR_PALETTE[hashed];
}
