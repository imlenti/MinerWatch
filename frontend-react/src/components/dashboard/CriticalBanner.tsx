import { Flame } from 'lucide-react';
import { Link } from 'react-router-dom';

import type { MinerListEntry, SettingsCurrent } from '@/lib/types';
import { fmtNum, isCanaanFamily } from '@/lib/format';

interface Props {
  miners: MinerListEntry[];
  settings: SettingsCurrent | null | undefined;
}

interface Hot {
  id: number;
  name: string;
  problems: string[];
}

/**
 * Persistent red bar at the top of the dashboard when at least one
 * online miner has chip or VR temperature above the configured
 * threshold. Mirrors the vanilla CriticalBanner — same thresholds,
 * same wording.
 */
export function CriticalBanner({ miners, settings }: Props) {
  if (!settings) return null;
  const chipMax = settings.alerts.temp_chip_threshold;
  const vrMax = settings.alerts.temp_vr_threshold;

  const hot: Hot[] = [];
  for (const m of miners) {
    if (!m.live_online) continue;
    const lm = m.last_metric;
    if (!lm) continue;
    const probs: string[] = [];
    if (lm.temp_chip_c !== null && lm.temp_chip_c !== undefined && lm.temp_chip_c >= chipMax) {
      probs.push(`chip ${fmtNum(lm.temp_chip_c, 1)}°C ≥ ${chipMax}°C`);
    }
    if (lm.temp_vr_c !== null && lm.temp_vr_c !== undefined && lm.temp_vr_c >= vrMax) {
      // Avalon (canaan) has no VR sensor — temp_vr_c carries the air
      // outlet temp, so the wording follows (same as the miner tiles).
      probs.push(`${isCanaanFamily(m.family) ? 'air out' : 'VR'} ${fmtNum(lm.temp_vr_c, 1)}°C ≥ ${vrMax}°C`);
    }
    if (probs.length) hot.push({ id: m.id, name: m.name, problems: probs });
  }

  if (!hot.length) return null;

  return (
    <div className="flex items-start gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
      <Flame className="h-5 w-5 shrink-0 mt-0.5" />
      <div className="flex-1">
        <span className="font-semibold">Critical status: </span>
        {hot.map((h, idx) => (
          <span key={h.id}>
            <Link to={`/miner/${h.id}`} className="font-semibold underline hover:no-underline">
              {h.name}
            </Link>
            <span className="text-destructive/80"> · {h.problems.join(', ')}</span>
            {idx < hot.length - 1 ? '; ' : ''}
          </span>
        ))}
      </div>
    </div>
  );
}
