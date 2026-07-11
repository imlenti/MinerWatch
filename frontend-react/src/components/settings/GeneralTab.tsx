import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { DashboardLayoutCard } from './DashboardLayoutCard';
import { HiddenTrophiesCard } from './HiddenTrophiesCard';
import type { SettingsFormState } from './SettingsForm';

interface Props {
  form: SettingsFormState;
  setForm: (next: SettingsFormState) => void;
}

export function GeneralTab({ form, setForm }: Props) {
  return (
    <div className="space-y-4">
      <DashboardLayoutCard />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Polling & storage</CardTitle>
          <CardDescription>How often miners are polled and how long history is kept.</CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field
            id="polling.interval_seconds"
            label="Polling interval (seconds)"
            value={form.pollingInterval}
            min={2}
            max={300}
            onChange={(v) => setForm({ ...form, pollingInterval: v })}
          />
          <Field
            id="polling.request_timeout"
            label="Request timeout (seconds)"
            value={form.requestTimeout}
            min={1}
            max={30}
            onChange={(v) => setForm({ ...form, requestTimeout: v })}
          />
          <Field
            id="polling.hashrate_smoothing_seconds"
            label="Hashrate smoothing (seconds, 0 = off)"
            value={form.hashrateSmoothing}
            min={0}
            max={600}
            onChange={(v) => setForm({ ...form, hashrateSmoothing: v })}
          />
          <Field
            id="storage.retention_days"
            label="History retention (days)"
            value={form.retentionDays}
            min={1}
            max={3650}
            onChange={(v) => setForm({ ...form, retentionDays: v })}
          />
          <Field
            id="guardian.hashrate_average_window_seconds"
            label="Guardian averaging window (seconds, 0 = off)"
            value={form.guardianHashrateAverageWindow}
            min={0}
            max={600}
            onChange={(v) => setForm({ ...form, guardianHashrateAverageWindow: v })}
          />
          <div className="text-xs text-muted-foreground sm:col-span-2 space-y-1.5">
            <div>
              <span className="font-semibold text-foreground">Hashrate smoothing</span>: tau (time constant) of the
              server-side EMA. 60s is a good trade-off between responsiveness and stability. Set to 0 to see raw
              firmware values.
            </div>
            <div>
              <span className="font-semibold text-foreground">Guardian averaging window</span>: The time window used by the
              Guardian governor to average hashrate, temperature, power, and hardware error percentage metrics. This
              prevents false-positive throttling from transient dips/peaks. Set to 0 to use instantaneous values.
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Network</CardTitle>
          <CardDescription>Subnet used by auto-discovery (Scan network on the Dashboard).</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label htmlFor="network.scan_cidr">
            Subnet scan (CIDR, "auto" for the host's network)
          </Label>
          <Input
            id="network.scan_cidr"
            type="text"
            placeholder="192.168.1.0/24"
            value={form.scanCidr}
            onChange={(e) => setForm({ ...form, scanCidr: e.target.value })}
          />
        </CardContent>
      </Card>
      <HiddenTrophiesCard />
    </div>
  );
}

interface FieldProps {
  id: string;
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (v: number) => void;
}

function Field({ id, label, value, min, max, step, onChange }: FieldProps) {
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="number"
        value={Number.isFinite(value) ? value : ''}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
