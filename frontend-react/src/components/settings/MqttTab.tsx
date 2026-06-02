import { useState } from 'react';
import { RadioTower, Send } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useMqttTest } from '@/api/hooks';
import { ApiError } from '@/lib/api';
import type { SettingsFormState } from './SettingsForm';

interface Props {
  form: SettingsFormState;
  setForm: (next: SettingsFormState) => void;
}

/**
 * MQTT / Home Assistant tab.
 *
 * MinerWatch connects to a broker you already run (e.g. the Mosquitto
 * add-on) and publishes one retained JSON state blob per miner, plus —
 * optionally — HA MQTT-discovery configs and scalar "flat" topics for an
 * ESP32/ESPHome panel. See docs/home-assistant-integration.md.
 *
 * The "Test connection" button checks the *saved* config, so the flow is
 * "Save all", then "Test" — same convention as the Telegram tab.
 */
export function MqttTab({ form, setForm }: Props) {
  const test = useMqttTest();
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function handleTest() {
    setError(null);
    setInfo(null);
    try {
      const r = await test.mutateAsync();
      setInfo(r.detail ?? 'Connected to the broker.');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-chart-performance/15 text-chart-performance">
            <RadioTower className="h-4 w-4" />
          </div>
          <div>
            <CardTitle className="text-base">MQTT / Home Assistant</CardTitle>
            <CardDescription>
              Publish miners to an MQTT broker — auto-discovered by Home Assistant, or consumed
              directly by an ESP32/ESPHome panel. Disabled by default.
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-muted/30 p-3">
          <div>
            <div className="text-sm font-semibold">Publish to MQTT</div>
            <p className="text-xs text-muted-foreground">
              MinerWatch connects out to your broker; it never opens a new inbound port.
            </p>
          </div>
          <Switch
            checked={form.mqttEnabled}
            onCheckedChange={(v) => setForm({ ...form, mqttEnabled: v })}
          />
        </div>

        {/* Connection status */}
        <div className="rounded-md border border-border bg-muted/40 p-3 text-sm">
          <span className="font-semibold">Status: </span>
          {form.mqttConnected ? (
            <span className="text-emerald-400">connected to the broker</span>
          ) : (
            <span className="text-muted-foreground">not connected</span>
          )}
          <span className="ml-1 text-xs text-muted-foreground">
            (refreshes on save / page reload)
          </span>
        </div>

        {/* Broker connection */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="space-y-2 sm:col-span-2">
            <Label htmlFor="mqtt.host">Broker host</Label>
            <Input
              id="mqtt.host"
              type="text"
              placeholder="e.g. localhost"
              autoComplete="off"
              value={form.mqttHost}
              onChange={(e) => setForm({ ...form, mqttHost: e.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="mqtt.port">Port</Label>
            <Input
              id="mqtt.port"
              type="number"
              value={form.mqttPort}
              onChange={(e) => setForm({ ...form, mqttPort: Number(e.target.value) })}
            />
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="mqtt.username">Username</Label>
            <Input
              id="mqtt.username"
              type="text"
              placeholder="(optional)"
              autoComplete="off"
              value={form.mqttUsername}
              onChange={(e) => setForm({ ...form, mqttUsername: e.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="mqtt.password">Password</Label>
            <Input
              id="mqtt.password"
              type="password"
              placeholder="••••••••"
              autoComplete="new-password"
              value={form.mqttPassword}
              onChange={(e) => setForm({ ...form, mqttPassword: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              {form.mqttPasswordSet
                ? '✓ password configured — leave empty to keep, fill in to replace'
                : 'optional — only if your broker requires auth'}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={handleTest} disabled={test.isPending}>
            <Send className="h-4 w-4" />
            {test.isPending ? 'Testing…' : 'Test connection'}
          </Button>
          <span className="text-xs text-muted-foreground">
            Click <strong>Save all</strong> first — Test checks the saved settings.
          </span>
        </div>

        {(info || error) && (
          <p className={`text-sm ${error ? 'text-destructive' : 'text-emerald-400'}`} role="status">
            {error ?? info}
          </p>
        )}

        {/* Topics & advanced */}
        <div className="grid grid-cols-1 gap-4 border-t border-border pt-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="mqtt.base_topic">Base topic</Label>
            <Input
              id="mqtt.base_topic"
              type="text"
              placeholder="minerwatch"
              autoComplete="off"
              value={form.mqttBaseTopic}
              onChange={(e) => setForm({ ...form, mqttBaseTopic: e.target.value })}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="mqtt.discovery_prefix">HA discovery prefix</Label>
            <Input
              id="mqtt.discovery_prefix"
              type="text"
              placeholder="homeassistant"
              autoComplete="off"
              value={form.mqttDiscoveryPrefix}
              onChange={(e) => setForm({ ...form, mqttDiscoveryPrefix: e.target.value })}
            />
          </div>
        </div>

        {/* Ambient temperature relay (optional) */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <Label htmlFor="mqtt.ambient_temp_topic">Ambient temperature topic</Label>
            <Input
              id="mqtt.ambient_temp_topic"
              type="text"
              placeholder="(optional) e.g. home/temperature"
              autoComplete="off"
              value={form.mqttAmbientTempTopic}
              onChange={(e) => setForm({ ...form, mqttAmbientTempTopic: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Plain-text Celsius value from another device on this broker. MinerWatch relays it to
              the ESP32 panel (60s average + session min/max). Empty = disabled.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="mqtt.ambient_temp_status_topic">Ambient status topic</Label>
            <Input
              id="mqtt.ambient_temp_status_topic"
              type="text"
              placeholder="(optional) online/offline"
              autoComplete="off"
              value={form.mqttAmbientStatusTopic}
              onChange={(e) => setForm({ ...form, mqttAmbientStatusTopic: e.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Optional online/offline topic, used only to mark the reading available.
            </p>
          </div>
        </div>

        <ToggleRow
          title="Home Assistant discovery"
          desc="Publish discovery configs so miners auto-appear as HA devices and entities."
          checked={form.mqttDiscoveryEnabled}
          onChange={(v) => setForm({ ...form, mqttDiscoveryEnabled: v })}
        />
        <ToggleRow
          title="Flat per-field topics (ESP32 / ESPHome)"
          desc="Also publish minerwatch/<mac>/f/<field> scalars, so a panel can read values without parsing JSON."
          checked={form.mqttFlatTopics}
          onChange={(v) => setForm({ ...form, mqttFlatTopics: v })}
        />
        <ToggleRow
          title="TLS"
          desc="Encrypt the broker connection (the port is usually 8883 then)."
          checked={form.mqttTls}
          onChange={(v) => setForm({ ...form, mqttTls: v })}
        />

        {/* Destructive — warn explicitly */}
        <div className="flex items-start justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
          <div>
            <div className="text-sm font-semibold text-amber-300">Allow remote control</div>
            <p className="text-xs text-amber-300/80">
              Exposes restart / fan / frequency / voltage command entities. These are destructive and
              interact with Guardian &amp; auto-fan. Leave off unless you understand the risk, and lock
              the command topics down with a broker ACL.
            </p>
          </div>
          <Switch
            checked={form.mqttAllowControls}
            onCheckedChange={(v) => setForm({ ...form, mqttAllowControls: v })}
          />
        </div>

        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Quick setup — broker &amp; ESP panel</summary>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-muted-foreground">
            <li>
              Run a broker on your LAN — e.g. the <strong>Mosquitto</strong> add-on in Home Assistant,
              or <code className="rounded bg-muted px-1">brew install mosquitto</code> on a Mac.
            </li>
            <li>
              Fill <strong>Broker host</strong> (its LAN IP) and credentials, tick <strong>Publish to
              MQTT</strong>, then click <strong>Save all</strong>.
            </li>
            <li>
              Click <strong>Test connection</strong> — you should see a success message.
            </li>
            <li>
              In Home Assistant the miners appear automatically (Settings → Devices). For a standalone
              ESP32 panel, enable <strong>Flat per-field topics</strong> instead.
            </li>
            <li>
              Verify from a shell:{' '}
              <code className="rounded bg-muted px-1">mosquitto_sub -h &lt;broker&gt; -t 'minerwatch/#' -v</code>.
            </li>
          </ol>
        </details>
      </CardContent>
    </Card>
  );
}

function ToggleRow({
  title,
  desc,
  checked,
  onChange,
}: {
  title: string;
  desc: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-muted/30 p-3">
      <div>
        <div className="text-sm font-semibold">{title}</div>
        <p className="text-xs text-muted-foreground">{desc}</p>
      </div>
      <Switch checked={checked} onCheckedChange={onChange} />
    </div>
  );
}
