import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Save } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Skeleton } from '@/components/ui/skeleton';
import { GeneralTab } from '@/components/settings/GeneralTab';
import { AlertsTab } from '@/components/settings/AlertsTab';
import { NotificationsTab } from '@/components/settings/NotificationsTab';
import { MqttTab } from '@/components/settings/MqttTab';
import { SecurityTab } from '@/components/settings/SecurityTab';
import { formToOverrides, useSettingsForm } from '@/components/settings/SettingsForm';
import { useSaveSettings, useSettings } from '@/api/hooks';
import { ApiError } from '@/lib/api';

/**
 * Settings page.
 *
 * Form state is held in `useSettingsForm` (a thin useState wrapper)
 * and seeded by the /api/settings response. Save sends a single POST
 * with all the dotted-key overrides. After a successful save the
 * write-only fields (telegramBotToken, authPassword) are cleared and
 * the settings query is invalidated so the rest of the app picks up
 * the new values.
 */
export function SettingsPage() {
  const { data, isLoading } = useSettings();
  const [form, setForm] = useSettingsForm(data?.current);
  const save = useSaveSettings();
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Deep-link support: /settings?tab=security opens straight on that tab
  // (used by the security banner / auto-scan warning). Falls back to
  // General for a missing or unrecognised value.
  const [searchParams] = useSearchParams();
  const validTabs = ['general', 'alerts', 'notifications', 'mqtt', 'security'];
  const tabParam = searchParams.get('tab');
  const initialTab =
    tabParam && validTabs.includes(tabParam) ? tabParam : 'general';

  async function handleSave() {
    if (!form) return;
    setMessage(null);
    setError(null);
    try {
      await save.mutateAsync(formToOverrides(form));
      // Clear the secrets so they don't linger in the DOM
      setForm({ ...form, telegramBotToken: '', authPassword: '', mqttPassword: '' });
      setMessage('Settings saved.');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    }
  }

  if (isLoading || !form) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
          <p className="text-sm text-muted-foreground">
            Configuration, alerts, notifications and security
          </p>
        </div>
        <Button onClick={handleSave} disabled={save.isPending}>
          <Save className="h-4 w-4" />
          {save.isPending ? 'Saving…' : 'Save all'}
        </Button>
      </header>

      {(message || error) && (
        <p
          className={`rounded-md border px-3 py-2 text-sm ${
            error
              ? 'border-destructive/40 bg-destructive/10 text-destructive'
              : 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
          }`}
          role="status"
        >
          {error ?? message}
        </p>
      )}

      <Tabs defaultValue={initialTab} className="space-y-4">
        <TabsList className="h-auto">
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="alerts">Alerts</TabsTrigger>
          <TabsTrigger value="notifications">Notifications</TabsTrigger>
          <TabsTrigger value="mqtt">MQTT</TabsTrigger>
          <TabsTrigger value="security">Security</TabsTrigger>
        </TabsList>

        <TabsContent value="general" className="mt-0">
          <GeneralTab form={form} setForm={setForm} />
        </TabsContent>
        <TabsContent value="alerts" className="mt-0">
          <AlertsTab form={form} setForm={setForm} />
        </TabsContent>
        <TabsContent value="notifications" className="mt-0">
          <NotificationsTab form={form} setForm={setForm} />
        </TabsContent>
        <TabsContent value="mqtt" className="mt-0">
          <MqttTab form={form} setForm={setForm} />
        </TabsContent>
        <TabsContent value="security" className="mt-0">
          <SecurityTab form={form} setForm={setForm} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
