import { useEffect, useRef, useState } from 'react';
import { Flame, Gauge, Leaf } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { ApiError } from '@/lib/api';
import { useSetWorkmode } from '@/api/hooks';
import type { MinerDetailResponse } from '@/lib/types';

interface Props {
  data: MinerDetailResponse;
}

interface ModeDef {
  value: number;
  icon: typeof Leaf;
  desc: string;
  // Full Tailwind class strings (kept literal so the JIT compiler keeps
  // them). `active` styles the selected button; `icon` keeps the accent
  // colour on the icon even when the button is idle, so the traffic-light
  // reading is always present without shouting.
  active: string;
  icon_cls: string;
}

// Mid uses the same amber the dashboard shows for a chip at ~67 °C
// (`tempTone` → 'warm' → text-amber-400), so the colour language matches
// the temperature readouts. Low = eco green, High = max red.
const MODES: ModeDef[] = [
  {
    value: 0,
    icon: Leaf,
    desc: 'eco · cooler',
    active: 'border-emerald-500/50 bg-emerald-500/10 text-emerald-400',
    icon_cls: 'text-emerald-400',
  },
  {
    value: 1,
    icon: Gauge,
    desc: 'balanced',
    active: 'border-amber-500/50 bg-amber-400/10 text-amber-400',
    icon_cls: 'text-amber-400',
  },
  {
    value: 2,
    icon: Flame,
    desc: 'max · hotter',
    active: 'border-red-500/50 bg-red-500/10 text-red-400',
    icon_cls: 'text-red-400',
  },
];

/**
 * Per-model labels for the three work-mode slots. The Nano 3s firmware
 * exposes Low/Mid/High; other Canaan models (e.g. the Mini 3, which the
 * vendor app labels Heater/Mining/Night) can be added here when they get
 * their own detail view, without touching the firmware command itself.
 */
function modeLabels(_model: string | null | undefined): [string, string, string] {
  return ['Low', 'Mid', 'High'];
}

/**
 * Controls tab — Avalon work mode (Low / Mid / High).
 *
 * This issues the firmware's own work-mode preset — ``ascset
 * 0,workmode,set,N`` on the Nano 3s / later Avalon, ``ascset
 * 0,worklevel,set,N`` on the original Nano 3 — so it stays inside the
 * vendor's blessed presets rather than poking raw frequency/voltage.
 * The change applies immediately; unlike a pool change it needs no reboot.
 *
 * Self-hides on families that don't advertise the capability.
 */
export function WorkModeControls({ data }: Props) {
  const { miner, capabilities } = data;
  const liveMode = data.live_sample?.workmode ?? null;
  const [selected, setSelected] = useState<number | null>(liveMode);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Last polled value we adopted. Lets us tell a real device-state change
  // apart from a re-render that just carries the same (possibly stale)
  // reading.
  const lastLiveRef = useRef<number | null>(liveMode);

  const setWorkmode = useSetWorkmode(miner.id);

  // Adopt the device's reported mode only when the polled value actually
  // changes — confirming our own set, or reflecting a change made
  // elsewhere. The click sets `selected` optimistically; a stale poll
  // right after it still carries the previous value, so without this
  // guard it would clobber the selection and the button would flip back
  // to the old mode for a moment until the next poll caught up.
  useEffect(() => {
    if (liveMode !== lastLiveRef.current) {
      lastLiveRef.current = liveMode;
      setSelected(liveMode);
    }
  }, [liveMode]);

  if (!capabilities.set_workmode) return null;

  const labels = modeLabels(data.live_sample?.model);

  async function apply(mode: number) {
    setFeedback(null);
    setError(null);
    setSelected(mode);
    try {
      await setWorkmode.mutateAsync({ mode });
      setFeedback(`Work mode set to ${labels[mode]}`);
    } catch (err) {
      setSelected(liveMode);
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    }
  }

  const pending = setWorkmode.isPending;
  const activeLabel = selected !== null ? labels[selected] : null;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="text-base">Work mode</CardTitle>
        {activeLabel && (
          <Badge variant="secondary" className="tabular-nums">
            Active: {activeLabel}
          </Badge>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-3 gap-2">
          {MODES.map((m) => {
            const isActive = selected === m.value;
            const Icon = m.icon;
            return (
              <button
                key={m.value}
                type="button"
                onClick={() => apply(m.value)}
                disabled={pending}
                aria-pressed={isActive}
                className={`flex flex-col items-center justify-center gap-1 rounded-md border px-3 py-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
                  isActive
                    ? m.active
                    : 'border-border bg-transparent text-foreground hover:bg-muted/50'
                }`}
              >
                <Icon className={`h-4 w-4 ${isActive ? '' : m.icon_cls}`} />
                <span>{labels[m.value]}</span>
                <span className={`text-xs ${isActive ? 'opacity-80' : 'text-muted-foreground'}`}>
                  {m.desc}
                </span>
              </button>
            );
          })}
        </div>

        <p className="text-xs text-muted-foreground">
          Sends the firmware's native work mode — the same preset the Avalon app uses.
          Applies immediately; no reboot needed.
        </p>

        {(feedback || error) && (
          <p className={`text-sm ${error ? 'text-destructive' : 'text-emerald-400'}`} role="status">
            {error ?? feedback}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
