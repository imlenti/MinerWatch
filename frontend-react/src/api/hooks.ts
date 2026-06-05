import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/lib/api';
import type {
  AlertsResponse,
  AuthStatus,
  BestRecordsResponse,
  BestRecordsTopResponse,
  BlockFindsResponse,
  DiscoveryResponse,
  DonationInfo,
  DonationListResponse,
  FleetHashrateResponse,
  GuardianStatusResponse,
  MetricsRangeResponse,
  MinerCreatePayload,
  MinerDetailResponse,
  MinerListResponse,
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

export function useBlockFinds() {
  return useQuery({
    queryKey: ['block-finds'],
    queryFn: ({ signal }) =>
      api<BlockFindsResponse>('/api/fleet/block_finds', { signal }),
    refetchInterval: 30_000, // block finds are rare, no need to hammer
  });
}

export function useAuthStatus() {
  return useQuery({
    queryKey: ['auth-status'],
    queryFn: ({ signal }) => api<AuthStatus>('/api/auth/status', { signal }),
    staleTime: Infinity, // doesn't change without user action
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

interface FanConfigPayload {
  fan_mode?: 'manual' | 'firmware' | 'minerwatch';
  auto_target_c?: number;
  fan_min_override?: number;
  fan_max_override?: number;
  fan_threshold_c?: number;
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

export function useMqttTest() {
  return useMutation({
    mutationFn: () =>
      api<{ ok: true; detail?: string }>('/api/mqtt/test', { method: 'POST' }),
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

export function useDonations() {
  return useQuery({
    queryKey: ['donations'],
    queryFn: ({ signal }) =>
      api<DonationListResponse>('/api/donations', { signal }),
    refetchInterval: 10_000,
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
