import { useEffect, useState } from 'react';
import { Activity, Cpu, Gauge, ShieldAlert, Zap } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Badge } from '@/components/ui/badge';
import { ApiError } from '@/lib/api';
import { useGuardianStatus, useSetGuardianConfig } from '@/api/hooks';
import type { MinerDetailResponse } from '@/lib/types';

interface Props {
  data: MinerDetailResponse;
}

/**
 * Advanced tab — the Guardian (runtime frequency governor).
 *
 * The Guardian is a slow, always-on loop that nudges ASIC frequency to keep a
 * temperature signal — the VR by default, or the ASIC chip per-miner — and the
 * HW error rate inside safe bounds, never above a per-miner "max frequency"
 * ceiling (default: the current frequency). It is frequency-only in v1. This
 * panel is the per-miner opt-in + the editable ceiling/floor, the temperature
 * source and max-temperature knobs, and a live readout of the loop's last
 * decision. Enabling it opens an at-your-own-risk confirmation first.
 */
export function GuardianPanel({ data }: Props) {
  const { miner, capabilities } = data;
  const status = useGuardianStatus(miner.id);
  const setConfig = useSetGuardianConfig(miner.id);

  const s = status.data;
  const currentFreq = s?.current_freq_mhz ?? null;

  // The "max frequency" field. Seeded from the stored ceiling, falling back
  // to the live current frequency (what it would default to on first enable).
  const [maxFreq, setMaxFreq] = useState<number | ''>('');
  const [floor, setFloor] = useState<number | ''>('');
  // Temperature source ('vr' | 'chip') and the per-miner max temperature.
  const [source, setSource] = useState<'vr' | 'chip'>('vr');
  const [maxTemp, setMaxTemp] = useState<number | ''>('');
  // At-your-own-risk confirmation, gating the enable toggle.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Sync the editable fields when the backend state arrives / changes.
  useEffect(() => {
    if (!s) return;
    setMaxFreq(s.max_freq_mhz ?? s.current_freq_mhz ?? '');
    setFloor(s.freq_floor_mhz ?? '');
    setSource(s.temp_source ?? 'vr');
    const defHigh =
      (s.temp_source ?? 'vr') === 'chip'
        ? s.defaults.chip_high_c
        : s.defaults.vr_high_c;
    setMaxTemp(s.max_temp_c ?? defHigh ?? '');
  }, [
    s?.max_freq_mhz,
    s?.freq_floor_mhz,
    s?.current_freq_mhz,
    s?.temp_source,
    s?.max_temp_c,
  ]);

  if (!capabilities.set_frequency) {
    return (
      <Card>
        <CardContent className="py-6 text-sm text-muted-foreground">
          The Guardian controls ASIC frequency, which this miner family does
          not expose over the API. It is available on Bitaxe and Nerd* miners.
        </CardContent>
      </Card>
    );
  }

  if (status.isLoading || !s) {
    return (
      <Card>
        <CardContent className="py-6 text-sm text-muted-foreground">
          Loading Guardian status…
        </CardContent>
      </Card>
    );
  }

  if (!s.supported) {
    return (
      <Card>
        <CardContent className="py-6 text-sm text-muted-foreground">
          The Guardian is only supported on Bitaxe / Nerd* miners.
        </CardContent>
      </Card>
    );
  }

  if (!s.enabled) {
    return (
      <Card>
        <CardContent className="py-6 text-sm text-muted-foreground">
          The Guardian feature is disabled globally (guardian.enabled = false).
        </CardContent>
      </Card>
    );
  }

  const enabled = s.miner_enabled;
  const d = s.defaults;
  const pending = setConfig.isPending;

  // Per-source default high threshold + hysteresis deadband, used to seed the
  // field, show the placeholder/default, and derive the recovery point.
  const defHighFor = (src: 'vr' | 'chip') =>
    src === 'chip' ? d.chip_high_c : d.vr_high_c;
  const deadbandFor = (src: 'vr' | 'chip') =>
    src === 'chip' ? d.chip_high_c - d.chip_low_c : d.vr_high_c - d.vr_low_c;

  const srcLabel = source === 'chip' ? 'Chip' : 'VR';
  const highC = typeof maxTemp === 'number' ? maxTemp : defHighFor(source);
  const lowC = Math.round((highC - deadbandFor(source)) * 10) / 10;
  const liveTemp = s.live?.temp_c ?? s.live?.vr_temp_c ?? null;

  async function run(
    payload: {
      enabled?: boolean;
      max_freq_mhz?: number;
      freq_floor_mhz?: number;
      temp_source?: 'vr' | 'chip';
      max_temp_c?: number;
    },
    ok: string,
  ) {
    setFeedback(null);
    setError(null);
    try {
      await setConfig.mutateAsync(payload);
      setFeedback(ok);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    }
  }

  async function toggleEnabled(next: boolean) {
    // When enabling, capture the max-frequency field so the ceiling is set
    // explicitly (the backend would otherwise default it to the current freq).
    const payload: { enabled: boolean; max_freq_mhz?: number } = { enabled: next };
    if (next && typeof maxFreq === 'number' && Number.isFinite(maxFreq)) {
      payload.max_freq_mhz = maxFreq;
    }
    await run(payload, next ? 'Guardian enabled' : 'Guardian disabled');
  }

  // The enable switch is gated by a confirmation: turning it ON opens the
  // dialog; turning it OFF applies immediately (no friction to back out).
  function onToggle(next: boolean) {
    if (next) setConfirmOpen(true);
    else void toggleEnabled(false);
  }

  async function confirmEnable() {
    setConfirmOpen(false);
    await toggleEnabled(true);
  }

  async function selectSource(next: 'vr' | 'chip') {
    if (next === source) return;
    setSource(next);
    // If the user hasn't pinned a per-miner max, reflect the new source's
    // default so the field and the derived recovery point stay sensible.
    if (s?.max_temp_c == null) setMaxTemp(defHighFor(next));
    await run(
      { temp_source: next },
      `Temperature source set to ${next === 'chip' ? 'ASIC chip' : 'VR'}`,
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <Gauge className="h-4 w-4" /> Guardian
        </CardTitle>
        <Badge
          variant={enabled ? 'success' : 'secondary'}
          className="flex items-center gap-1.5"
        >
          <Activity className="h-3 w-3" /> {enabled ? 'Active' : 'Off'}
        </Badge>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* What it does */}
        <p className="text-sm text-muted-foreground">
          A slow, always-on governor that adapts ASIC <strong>frequency</strong>{' '}
          to the heat. It backs off when temperature climbs or errors climb, and
          recovers frequency when things cool — never going above your max.
          Ideal for summer, when ambient swings within a single day.
        </p>

        {/* Enable toggle */}
        <div className="flex items-center justify-between border-t border-border pt-4">
          <div className="space-y-0.5">
            <Label className="text-sm">Enable Guardian on this miner</Label>
            <p className="text-xs text-muted-foreground">
              Re-evaluates every {d.interval_seconds}s. Changes apply live (no
              reboot).
            </p>
          </div>
          <Switch
            checked={enabled}
            disabled={pending}
            onCheckedChange={onToggle}
          />
        </div>

        {/* Max frequency (editable ceiling) */}
        <div className="space-y-2 border-t border-border pt-4">
          <Label htmlFor="guardian-max" className="text-sm">
            Max frequency (MHz)
          </Label>
          <div className="flex gap-2">
            <Input
              id="guardian-max"
              type="number"
              min={100}
              max={2000}
              step={5}
              value={maxFreq}
              onChange={(e) =>
                setMaxFreq(e.target.value === '' ? '' : Number(e.target.value))
              }
              disabled={pending}
              className="max-w-[140px]"
            />
            <Button
              variant="subtle"
              disabled={pending || maxFreq === ''}
              onClick={() =>
                typeof maxFreq === 'number' &&
                run({ max_freq_mhz: maxFreq }, `Max frequency set to ${maxFreq} MHz`)
              }
            >
              Save max
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            The ceiling the Guardian never exceeds. Defaults to the current
            frequency
            {currentFreq != null ? ` (${currentFreq} MHz)` : ''}; raise it only
            if you know your hardware sustains it.
          </p>
        </div>

        {/* Temperature source (VR vs ASIC chip) */}
        <div className="space-y-2 border-t border-border pt-4">
          <Label className="text-sm">Temperature source</Label>
          <div className="inline-flex overflow-hidden rounded-md border border-border">
            <button
              type="button"
              disabled={pending}
              onClick={() => selectSource('chip')}
              className={`flex items-center gap-1.5 px-4 py-1.5 text-sm transition-colors disabled:opacity-60 ${
                source === 'chip'
                  ? 'bg-primary/15 font-medium text-primary'
                  : 'text-muted-foreground hover:bg-muted/50'
              }`}
            >
              <Cpu className="h-4 w-4" /> ASIC chip
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => selectSource('vr')}
              className={`flex items-center gap-1.5 border-l border-border px-4 py-1.5 text-sm transition-colors disabled:opacity-60 ${
                source === 'vr'
                  ? 'bg-primary/15 font-medium text-primary'
                  : 'text-muted-foreground hover:bg-muted/50'
              }`}
            >
              <Zap className="h-4 w-4" /> VR
            </button>
          </div>
          <p className="text-xs text-muted-foreground">
            Which sensor drives frequency. <strong>VR</strong> (recommended):
            no other loop governs it. <strong>Chip</strong>: already regulated
            by the fan and the 75°C watchdog — it only acts once the fan is
            saturated.
          </p>
        </div>

        {/* Max temperature (per-miner high threshold; recovery derived) */}
        <div className="space-y-2 border-t border-border pt-4">
          <Label htmlFor="guardian-maxtemp" className="text-sm">
            Max temperature (°C)
          </Label>
          <div className="flex items-center gap-2">
            <Input
              id="guardian-maxtemp"
              type="number"
              min={40}
              max={110}
              step={1}
              value={maxTemp}
              onChange={(e) =>
                setMaxTemp(e.target.value === '' ? '' : Number(e.target.value))
              }
              disabled={pending}
              className="max-w-[100px]"
            />
            <Button
              variant="subtle"
              disabled={pending || maxTemp === ''}
              onClick={() =>
                typeof maxTemp === 'number' &&
                run({ max_temp_c: maxTemp }, `Max temperature set to ${maxTemp}°C`)
              }
            >
              Save temp
            </Button>
            <span className="text-xs text-muted-foreground">
              recovers at ~{lowC}°C
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            The {srcLabel} threshold above which it cuts frequency; recovery is
            derived at max − {deadbandFor(source)}°C. Default for {srcLabel}:{' '}
            {defHighFor(source)}°C.
            {source === 'chip'
              ? ' Keep it below the 75°C overheat watchdog.'
              : ''}
          </p>
        </div>

        {/* Frequency floor (optional override) */}
        <div className="space-y-2 border-t border-border pt-4">
          <Label htmlFor="guardian-floor" className="text-sm">
            Frequency floor (MHz)
          </Label>
          <div className="flex gap-2">
            <Input
              id="guardian-floor"
              type="number"
              min={100}
              max={2000}
              step={5}
              value={floor}
              placeholder={String(d.frequency_floor_mhz)}
              onChange={(e) =>
                setFloor(e.target.value === '' ? '' : Number(e.target.value))
              }
              disabled={pending}
              className="max-w-[140px]"
            />
            <Button
              variant="subtle"
              disabled={pending || floor === ''}
              onClick={() =>
                typeof floor === 'number' &&
                run({ freq_floor_mhz: floor }, `Floor set to ${floor} MHz`)
              }
            >
              Save floor
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            The Guardian never throttles below this. Leave empty to use the
            global default ({d.frequency_floor_mhz} MHz).
          </p>
        </div>

        {/* Policy summary */}
        <div className="space-y-1 border-t border-border pt-4 text-xs text-muted-foreground">
          <p className="font-semibold text-foreground">Policy</p>
          <p>
            {srcLabel} &gt; {highC}°C → −{d.step_down_vr_mhz} MHz · Rejected
            shares &gt; {d.reject_pct_max}% → −{d.step_down_err_mhz} MHz ·{' '}
            {srcLabel} &lt; {lowC}°C → +{d.step_up_mhz} MHz (up to your max).
            Otherwise it holds.
          </p>
        </div>

        {/* Live readout */}
        <div className="space-y-2 border-t border-border pt-4">
          <p className="text-sm font-semibold">Live</p>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
            <Stat label="Frequency" value={fmt(s.live?.frequency_mhz ?? currentFreq, 'MHz')} />
            <Stat label={`${srcLabel} temp`} value={fmt(liveTemp, '°C')} />
            <Stat label="Reject" value={fmt(s.live?.reject_pct ?? null, '%')} />
            <Stat label="Ceiling" value={fmt(s.live?.ceiling_mhz ?? s.max_freq_mhz, 'MHz')} />
            <Stat label="Floor" value={fmt(s.live?.floor_mhz ?? s.freq_floor_mhz, 'MHz')} />
          </div>
          {s.live?.reason && (
            <p className="text-xs text-muted-foreground">
              Last decision: {s.live.reason}
            </p>
          )}
          {!s.live && enabled && (
            <p className="text-xs text-muted-foreground">
              Waiting for the first evaluation…
            </p>
          )}
        </div>

        {/* Risk note */}
        <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200/90">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            The Guardian changes your miner's frequency automatically. It only
            ever lowers below — and recovers up to — your max, and the 75°C
            overheat watchdog stays armed underneath it. In <strong>chip</strong>{' '}
            mode it shares the sensor with the fan PID and that watchdog, so pick
            the threshold with that in mind. Run it at your own risk and keep an
            eye on the miner, especially right after enabling.
          </span>
        </div>

        {(feedback || error) && (
          <p
            className={`text-sm ${error ? 'text-destructive' : 'text-emerald-400'}`}
            role="status"
          >
            {error ?? feedback}
          </p>
        )}
      </CardContent>

      {/* At-your-own-risk confirmation, shown when enabling the Guardian. */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ShieldAlert className="h-5 w-5 text-amber-400" /> Enable Guardian?
            </DialogTitle>
            <DialogDescription asChild>
              <div className="space-y-3 pt-1 text-sm text-muted-foreground">
                <p>
                  The Guardian automatically changes your miner's ASIC frequency
                  while it runs. This is an advanced feature that you enable{' '}
                  <strong className="text-foreground">at your own risk</strong>.
                </p>
                <p>
                  We strongly recommend staying near the miner and watching it
                  closely,{' '}
                  <strong className="text-foreground">
                    especially the first time
                  </strong>{' '}
                  you turn it on. The 75°C overheat watchdog stays armed
                  underneath, but you remain responsible for your hardware.
                </p>
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="subtle" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={pending}
              onClick={confirmEnable}
              className="gap-1.5"
            >
              <ShieldAlert className="h-4 w-4" /> I understand — enable
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-semibold tabular-nums">{value}</div>
    </div>
  );
}

function fmt(v: number | null | undefined, unit: string): string {
  if (v == null || !Number.isFinite(v)) return '—';
  return `${v} ${unit}`;
}
