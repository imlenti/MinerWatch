import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type {
  AlertsResponse,
  AmbientFleet,
  AmbientHistoryResponse,
  AuthStatus,
  BestRecordsResponse,
  BestRecordsTopResponse,
  BlockFindsResponse,
  DashboardLayoutResponse,
  DiscoveryResponse,
  DonationInfo,
  DonationListResponse,
  FleetHashrateResponse,
  GuardianStatusResponse,
  MetricsRangeResponse,
  MinerCreatePayload,
  MinerDetailResponse,
  MinerListResponse,
  MinedCoin,
  MinerOrderResponse,
  NotableSharesResponse,
  PoolsResponse,
  PredictionCoin,
  PredictionResponse,
  PushTestResponse,
  SettingsResponse,
  StartDonationResponse,
  SystemInfo,
  SystemSnapshot,
  TelegramDiscoverResponse,
  UpdateCheckResponse,
  UpdateInstallResponse,
  VersionResponse,
  WhatsNewResponse,
} from '@/lib/types';

// React Query hooks wrapping the /api endpoints MinerWatch exposes.
// Every hook owns its own polling cadence; if a screen mounts the same
// hook twice (e.g. a sidebar and a panel both reading the miner list)
// Query dedupes the network call automatically — that's the whole
// point of moving away from manual setInterval-based polling.
//
// Standard refetch is 5s, matching the backend poller cadence. Pages
// that don't need that frequency (Settings, Login) won't mount these.

const FIVE_SECONDS = 5_000;

export function useMiners() {
  return useQuery({
    queryKey: ['miners'],
    queryFn: ({ signal }) => api<MinerListResponse>('/api/miners', { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

// Persisted fleet display order (see `useMinerOrder` for the consumer).
// The backend applies the same list to the `/api/panel` feed,
// so the ESP32 panel mirrors the dashboard arrangement. Slow cadence on
// purpose: the order only changes when someone drags a card, and the
// default refetch-on-focus already covers the cross-window case.
export function useMinerOrderQuery() {
  return useQuery({
    queryKey: ['miner-order'],
    queryFn: ({ signal }) => api<MinerOrderResponse>('/api/miners/order', { signal }),
    refetchInterval: 30_000,
  });
}

// Fleet-wide flat list of (miner, pool slot) rows — drives the Pools
// page. Same cadence as useMiners; the backend reads this from the
// in-memory poll cache so there's no DB cost.
export function usePools() {
  return useQuery({
    queryKey: ['pools'],
    queryFn: ({ signal }) => api<PoolsResponse>('/api/pools', { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

export function useMiner(id: number | undefined) {
  return useQuery({
    enabled: Number.isInteger(id),
    queryKey: ['miner', id],
    queryFn: ({ signal }) =>
      api<MinerDetailResponse>(`/api/miners/${id}`, { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

export function useMinerMetrics(id: number | undefined, fromTs: number, toTs: number) {
  return useQuery({
    enabled: Number.isInteger(id) && fromTs < toTs,
    queryKey: ['miner-metrics', id, fromTs, toTs],
    queryFn: ({ signal }) =>
      api<MetricsRangeResponse>(
        `/api/miners/${id}/metrics?from_ts=${fromTs}&to_ts=${toTs}`,
        { signal },
      ),
    // Metric ranges are bigger payloads (up to 30 days of 1-min rollups)
    // so we keep them around longer than fleet polling.
    staleTime: 60_000,
    // The History window slides forward in time, so its query key changes
    // whenever the caller advances `now` (or the user switches range). Keep
    // serving the previous range's data until the new one lands instead of
    // returning undefined — that's what stops the charts flashing to a
    // skeleton and losing the active tooltip on every refresh.
    placeholderData: keepPreviousData,
  });
}

// Stored ambient (room) temperature series for ONE sensor over the same
// window as the per-miner metrics, so the History "Temperature" chart can
// overlay the miner's assigned room. `sensorId` selects which sensor; when
// it is null/undefined the query is disabled and no ambient line is drawn
// (a miner with no room assigned shows none). Same staleTime as
// useMinerMetrics — these are range payloads, not the live 5s snapshot
// (that's useAmbientTemp).
export function useAmbientHistory(
  fromTs: number,
  toTs: number,
  sensorId?: string | null,
) {
  return useQuery({
    enabled: fromTs < toTs && !!sensorId,
    queryKey: ['ambient-history', fromTs, toTs, sensorId ?? null],
    queryFn: ({ signal }) =>
      api<AmbientHistoryResponse>(
        `/api/fleet/ambient_temp/history?from_ts=${fromTs}&to_ts=${toTs}` +
          `&sensor_id=${encodeURIComponent(sensorId as string)}`,
        { signal },
      ),
    staleTime: 60_000,
    // Same reasoning as useMinerMetrics: keep the prior window's overlay on
    // screen while the next one loads so the Temperature chart never blanks.
    placeholderData: keepPreviousData,
  });
}

export function useFleetHashrate(minutes = 60, bucketSeconds = 60) {
  return useQuery({
    queryKey: ['fleet-hashrate', minutes, bucketSeconds],
    queryFn: ({ signal }) =>
      api<FleetHashrateResponse>(
        `/api/fleet/hashrate_history?minutes=${minutes}&bucket_seconds=${bucketSeconds}`,
        { signal },
      ),
    refetchInterval: FIVE_SECONDS,
  });
}

export function useFleetBest() {
  return useQuery({
    queryKey: ['fleet-best'],
    queryFn: ({ signal }) =>
      api<BestRecordsResponse>('/api/fleet/best_difficulty', { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

export function useFleetBestTop(scope: 'session' | 'alltime' = 'alltime', limit = 10) {
  return useQuery({
    queryKey: ['fleet-best-top', scope, limit],
    queryFn: ({ signal }) =>
      api<BestRecordsTopResponse>(
        `/api/fleet/best_difficulty/top?scope=${scope}&limit=${limit}`,
        { signal },
      ),
    refetchInterval: FIVE_SECONDS,
  });
}

// Near-block Hall of Fame for one miner (AxeOS only). Fed by the live
// log streamer and persisted, so it survives restarts. Refetched on a
// relaxed cadence — new entries are rare.
export function useMinerNotableShares(id: number | undefined, limit = 25) {
  return useQuery({
    enabled: Number.isInteger(id),
    queryKey: ['notable-shares', id, limit],
    queryFn: ({ signal }) =>
      api<NotableSharesResponse>(`/api/miners/${id}/notable_shares?limit=${limit}`, { signal }),
    refetchInterval: 15_000,
  });
}

export function useFleetPrediction(coin: PredictionCoin = 'auto') {
  return useQuery({
    queryKey: ['fleet-prediction', coin],
    queryFn: ({ signal }) =>
      api<PredictionResponse>(`/api/fleet/prediction?coin=${coin}`, { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

// Once-per-version "What's new" content. Static per release, so it's
// cached hard; the dialog decides client-side whether to show it.
export function useWhatsNew() {
  return useQuery({
    queryKey: ['whatsnew'],
    queryFn: ({ signal }) => api<WhatsNewResponse>('/api/whatsnew', { signal }),
    staleTime: Infinity,
  });
}

// `includeHidden` is the Settings view (lists dismissed trophies for
// restore); the dashboard keeps the default and never sees hidden rows.
export function useBlockFinds(includeHidden = false) {
  return useQuery({
    queryKey: ['block-finds', includeHidden],
    queryFn: ({ signal }) =>
      api<BlockFindsResponse>(
        `/api/fleet/block_finds${includeHidden ? '?include_hidden=true' : ''}`,
        { signal },
      ),
    refetchInterval: 30_000, // block finds are rare, no need to hammer
  });
}

// One trophy per call, by design: there is no bulk-hide. Invalidates
// every ['block-finds', *] variant so dashboard and Settings stay in
// sync after a hide/restore.
export function useSetBlockFindHidden() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, hidden }: { id: number; hidden: boolean }) =>
      api<{ ok: boolean; id: number; hidden: boolean }>(
        `/api/fleet/block_finds/${id}/${hidden ? 'hide' : 'unhide'}`,
        { method: 'POST' },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['block-finds'] });
    },
  });
}

// Ambient temperature pushed by external sensors — one entry per sensor.
// Polled at the standard 5s fleet cadence; the backend read is a cheap
// in-memory snapshot.
export function useAmbientTemp() {
  return useQuery({
    queryKey: ['fleet-ambient-temp'],
    queryFn: ({ signal }) =>
      api<AmbientFleet>('/api/fleet/ambient_temp', { signal }),
    refetchInterval: FIVE_SECONDS,
  });
}

export function useAuthStatus() {
  return useQuery({
    queryKey: ['auth-status'],
    queryFn: ({ signal }) => api<AuthStatus>('/api/auth/status', { signal }),
    staleTime: Infinity, // doesn't change without user action
  });
}

// Records that the operator opted out of the auto-scan security warning.
// Invalidates auth-status afterwards so scan_ack flips to true in the UI
// (auth-status has staleTime Infinity, so it won't refetch on its own).
export function useAckUnprotected() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api('/api/auth/ack_unprotected', { method: 'POST' }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auth-status'] });
    },
  });
}

export function useSettings() {
  return useQuery({
    queryKey: ['settings'],
    queryFn: ({ signal }) => api<SettingsResponse>('/api/settings', { signal }),
    // Settings change rarely (only when the user saves), but we still
    // pick up the new polling interval after a save without a full
    // reload by refetching every 30 s.
    refetchInterval: 30_000,
  });
}

export function useUnackAlerts() {
  return useQuery({
    queryKey: ['alerts', 'unack'],
    queryFn: ({ signal }) =>
      api<AlertsResponse>('/api/alerts?only_unack=true&limit=20', { signal }),
    refetchInterval: 10_000,
  });
}

// ---------- Mutations ----------
//
// Standard pattern: each mutation invalidates the queries whose data
// can be affected, so the UI reflects the change without manual
// re-fetches.

export function useAddMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: MinerCreatePayload) =>
      api<{ id: number }>('/api/miners', { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

export function useDeleteMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<{ deleted: number }>(`/api/miners/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

export function useSaveMinerOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) =>
      api<MinerOrderResponse>('/api/miners/order', { method: 'POST', body: { order } }),
    // Optimistic: patch the cache before the POST resolves, so a
    // 30s/focus refetch landing mid-drag can't bounce the grid back
    // to the previous arrangement.
    onMutate: async (order: string[]) => {
      await qc.cancelQueries({ queryKey: ['miner-order'] });
      qc.setQueryData<MinerOrderResponse>(['miner-order'], { order });
    },
    // The server may return a *merged* list (slots of temporarily
    // removed miners are preserved) — adopt it as the new truth.
    onSuccess: (data) => {
      qc.setQueryData<MinerOrderResponse>(['miner-order'], data);
    },
  });
}

export function useResetMinerOrder() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<MinerOrderResponse>('/api/miners/order', { method: 'DELETE' }),
    onSuccess: (data) => {
      qc.setQueryData<MinerOrderResponse>(['miner-order'], data);
    },
  });
}

// Persisted order of the main dashboard's movable sections (see
// `useDashboardLayout`). Frontend-only cosmetic preference — not shared
// with the ESP32 panel. Slow cadence: it only changes on a drag, and
// refetch-on-focus covers the cross-window case.
export function useDashboardLayoutQuery() {
  return useQuery({
    queryKey: ['dashboard-layout'],
    queryFn: ({ signal }) => api<DashboardLayoutResponse>('/api/dashboard/layout', { signal }),
    refetchInterval: 30_000,
  });
}

export function useSaveDashboardLayout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) =>
      api<DashboardLayoutResponse>('/api/dashboard/layout', { method: 'POST', body: { order } }),
    // Optimistic, like the miner order: patch the cache before the POST
    // resolves so a 30s/focus refetch landing mid-drag can't bounce the
    // arrangement back.
    onMutate: async (order: string[]) => {
      await qc.cancelQueries({ queryKey: ['dashboard-layout'] });
      qc.setQueryData<DashboardLayoutResponse>(['dashboard-layout'], { order });
    },
    onSuccess: (data) => {
      qc.setQueryData<DashboardLayoutResponse>(['dashboard-layout'], data);
    },
  });
}

export function useResetDashboardLayout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<DashboardLayoutResponse>('/api/dashboard/layout', { method: 'DELETE' }),
    onSuccess: (data) => {
      qc.setQueryData<DashboardLayoutResponse>(['dashboard-layout'], data);
    },
  });
}

export function useScanNetwork() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api<DiscoveryResponse>('/api/discovery/auto', { method: 'POST' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

export function useRestartMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      api(`/api/miners/${id}/control/restart`, { method: 'POST' }),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['miner', id] });
    },
  });
}

// Standby: stop hashing / power down the ASIC (AxeOS pause). Resume brings
// it back. Both invalidate the miner detail so the Standby badge flips on
// the next poll.
export function usePauseMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      api(`/api/miners/${id}/control/pause`, { method: 'POST' }),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['miner', id] });
    },
  });
}

export function useResumeMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      api(`/api/miners/${id}/control/resume`, { method: 'POST' }),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['miner', id] });
    },
  });
}

// NerdQAxe standby: POST /api/system/shutdown (powers down the ASIC). No
// soft resume on this firmware — the caller resumes with useRestartMiner.
export function useShutdownMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      api(`/api/miners/${id}/control/shutdown`, { method: 'POST' }),
    onSuccess: (_data, id) => {
      qc.invalidateQueries({ queryKey: ['miner', id] });
    },
  });
}

// Assign which ambient sensor (room) a miner sits in — pass null to clear.
// Invalidates the miner detail so the History chart re-resolves its overlay.
export function useSetAmbientSensor(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (assignment: { sensorId: string | null; name: string | null }) =>
      api(`/api/miners/${minerId}/ambient-sensor`, {
        method: 'POST',
        body: { sensor_id: assignment.sensorId, name: assignment.name },
      }),
    onSuccess: () => {
      // Refresh both the miner detail (History overlay) and the fleet list
      // (the dashboard card's read-only room label) so both reflect the change.
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

// Pin which SHA-256 coin a miner mines — pass null to go back to
// auto-detection. Only needed for firmware that reports no stratum network
// difficulty (Braiins, LuxOS, Canaan) on a pool whose URL and payout
// address give nothing away; everything else classifies itself.
//
// Invalidates the pools table (where the badge lives), the miner detail
// and the fleet list, plus the prediction so the Analytics sub-tabs
// re-group immediately instead of after the next poll.
export function useSetMinerCoin(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (coin: MinedCoin | null) =>
      api(`/api/miners/${minerId}/coin`, {
        method: 'POST',
        body: { coin },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pools'] });
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
      qc.invalidateQueries({ queryKey: ['miners'] });
      qc.invalidateQueries({ queryKey: ['fleet-prediction'] });
    },
  });
}

interface FanPayload {
  percent: number;
}

export function useSetFan(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: FanPayload) =>
      api(`/api/miners/${minerId}/control/fan`, { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
    },
  });
}

interface WorkModePayload {
  mode: number;
}

export function useSetWorkmode(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: WorkModePayload) =>
      api(`/api/miners/${minerId}/control/workmode`, { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
    },
  });
}

interface FanConfigPayload {
  fan_mode?: 'manual' | 'firmware' | 'minerwatch';
  auto_target_c?: number;
  fan_min_override?: number;
  fan_max_override?: number;
  fan_threshold_c?: number;
  watchdog_overheat_c?: number;
}

export function useSetFanConfig(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: FanConfigPayload) =>
      api(`/api/miners/${minerId}/control/fan_config`, { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
    },
  });
}

export function useAckAllAlerts() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { alerts } = await api<AlertsResponse>('/api/alerts?only_unack=true&limit=200');
      await Promise.all(
        alerts.map((a) => api(`/api/alerts/${a.id}/ack`, { method: 'POST' })),
      );
      return alerts.length;
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alerts'] });
    },
  });
}

// ---------- Settings page hooks ----------

export function useAllAlerts(limit = 50) {
  return useQuery({
    queryKey: ['alerts', 'all', limit],
    queryFn: ({ signal }) =>
      api<AlertsResponse>(`/api/alerts?limit=${limit}`, { signal }),
    refetchInterval: 15_000,
  });
}

export function useAckAlert() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api(`/api/alerts/${id}/ack`, { method: 'POST' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alerts'] });
    },
  });
}

// Silence a miner's offline/disconnect alert until it reconnects — backs the
// "Mute" button on offline rows in the AlertsBanner (the miner was powered
// down on purpose). The backend also acks that miner's offline rows, so
// invalidating ['alerts'] drops them out of the unread banner immediately.
// The mute clears itself the next time the miner is polled online again.
export function useMuteMinerOffline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (minerId: number) =>
      api<{ ok: true; miner_id: number; muted: boolean; acked: number }>(
        `/api/miners/${minerId}/offline-mute`,
        { method: 'POST' },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alerts'] });
      // Also refresh the fleet list so the card's "muted" badge appears at
      // once, instead of waiting for the next 5s miners poll.
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (overrides: Record<string, unknown>) =>
      api('/api/settings', { method: 'POST', body: { overrides } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] });
    },
  });
}

export function useTestPush() {
  return useMutation({
    mutationFn: () => api<PushTestResponse>('/api/push/test', { method: 'POST' }),
  });
}

export function usePurgeAllPush() {
  return useMutation({
    mutationFn: () =>
      api<{ ok: true; removed: number }>('/api/push/subscriptions/all', {
        method: 'DELETE',
      }),
  });
}

export function useTelegramTest() {
  return useMutation({
    mutationFn: () => api('/api/telegram/test', { method: 'POST' }),
  });
}

export function useTelegramDiscover() {
  return useMutation({
    mutationFn: () =>
      api<TelegramDiscoverResponse>('/api/telegram/discover_chat_id'),
  });
}

export function useLogout() {
  return useMutation({
    mutationFn: () => api('/api/auth/logout', { method: 'POST' }),
  });
}

// ---------- Guardian (runtime frequency governor) hooks ----------
//
// Status carries the live readout the loop publishes each tick. The
// governor itself runs on a slow cadence (minutes), but we poll the status
// a bit faster so the panel feels responsive after the user changes a
// setting; the backend read is cheap (in-memory + one DB row).

export function useGuardianStatus(id: number | undefined) {
  return useQuery({
    enabled: Number.isInteger(id),
    queryKey: ['guardian-status', id],
    queryFn: ({ signal }) =>
      api<GuardianStatusResponse>(`/api/miners/${id}/guardian/status`, { signal }),
    refetchInterval: 15_000,
  });
}

interface GuardianConfigPayload {
  enabled?: boolean;
  max_freq_mhz?: number;
  freq_floor_mhz?: number;
  temp_source?: 'vr' | 'chip';
  max_temp_c?: number;
  voltage_enabled?: boolean;
}

export function useSetGuardianConfig(minerId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: GuardianConfigPayload) =>
      api<{ ok: true; max_freq_mhz: number | null }>(
        `/api/miners/${minerId}/guardian/config`,
        { method: 'POST', body: payload },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['guardian-status', minerId] });
      qc.invalidateQueries({ queryKey: ['miner', minerId] });
    },
  });
}

// ---------- Donate hashrate hooks ----------
//
// useDonationInfo: static address/worker/bounds — never changes within a
//   session, so it's cached hard.
// useDonations: the active-donations table. Polled at 10s; the backend
//   read is cheap (one indexed query + the in-memory poll cache).
// Mutations invalidate ['donations'] so the table updates right after a
//   start / STOP without a manual refetch.

export function useDonationInfo() {
  return useQuery({
    queryKey: ['donation-info'],
    queryFn: ({ signal }) => api<DonationInfo>('/api/donations/info', { signal }),
    staleTime: Infinity,
  });
}

// `enabled` lets occasional consumers (e.g. the StarAskBanner, which
// only needs to know whether a donation exists while its donate ask is
// pending) mount the hook without adding a permanent 10s poll to pages
// that don't show the table. Defaults to true, so existing call sites
// are unaffected.
export function useDonations(enabled = true) {
  return useQuery({
    queryKey: ['donations'],
    queryFn: ({ signal }) =>
      api<DonationListResponse>('/api/donations', { signal }),
    refetchInterval: 10_000,
    enabled,
  });
}

interface StartDonationPayload {
  miner_ids: number[];
  hours: number;
}

export function useStartDonation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: StartDonationPayload) =>
      api<StartDonationResponse>('/api/donations', { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['donations'] });
      qc.invalidateQueries({ queryKey: ['miners'] });
    },
  });
}

export function useStopDonation() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (donationId: number) =>
      api<{ ok: true; reverted: number }>(`/api/donations/${donationId}/stop`, {
        method: 'POST',
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['donations'] });
    },
  });
}

export function useStopDonationMiner() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ donationId, dmId }: { donationId: number; dmId: number }) =>
      api<{ ok: boolean }>(`/api/donations/${donationId}/miners/${dmId}/stop`, {
        method: 'POST',
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['donations'] });
    },
  });
}

// ---------- System page hooks ----------

export function useSystemInfo() {
  return useQuery({
    queryKey: ['system', 'info'],
    queryFn: ({ signal }) => api<SystemInfo>('/api/system/info', { signal }),
    staleTime: 5 * 60_000, // hardware info never changes within a session
  });
}

export function useSystemSnapshot() {
  return useQuery({
    queryKey: ['system', 'snapshot'],
    queryFn: ({ signal }) => api<SystemSnapshot>('/api/system/snapshot', { signal }),
    refetchInterval: 5_000,
  });
}

interface SystemFanPayload {
  percent?: number;
  state?: number;
}

export function useSetSystemFan() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (payload: SystemFanPayload) =>
      api('/api/system/fan', { method: 'POST', body: payload }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['system', 'snapshot'] });
    },
  });
}

// ---------- Self-update hooks ----------
//
// useVersion: tiny `{version, system}` payload. Used by the footer and
//   the Update page header. ``staleTime: Infinity`` because the value
//   only changes after a restart — we re-fetch on focus instead.
//
// useUpdateCheck: drives the sidebar badge and the Update page body.
//   Refetched every 30 minutes; the backend itself caches the GitHub
//   response for 6 hours so this hook is cheap on the wire.
//
// useInstallUpdate: kicks off the install. The mutation returns when
//   the *response* lands (status: "restarting"); the process exits
//   shortly after, and the Update page is responsible for polling
//   /api/version until it answers again.

export function useVersion() {
  return useQuery({
    queryKey: ['version'],
    queryFn: ({ signal }) => api<VersionResponse>('/api/version', { signal }),
    staleTime: Infinity,
    refetchOnWindowFocus: true,
    // Silent failure (e.g. backend just restarted): keep the previous
    // value rather than tripping the page's error boundary.
    retry: 2,
  });
}

export function useUpdateCheck() {
  return useQuery({
    queryKey: ['update-check'],
    queryFn: ({ signal }) =>
      api<UpdateCheckResponse>('/api/update/check', { signal }),
    // 30 min between background polls. The backend cache means most
    // of these are no-ops on the network.
    refetchInterval: 30 * 60 * 1000,
    staleTime: 5 * 60 * 1000,
    retry: 1,
  });
}

export function useForceUpdateCheck() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api<UpdateCheckResponse>('/api/update/check?force=true'),
    onSuccess: (data) => {
      qc.setQueryData(['update-check'], data);
    },
  });
}

export function useInstallUpdate() {
  return useMutation({
    mutationFn: () =>
      api<UpdateInstallResponse>('/api/update/install', { method: 'POST' }),
  });
}
