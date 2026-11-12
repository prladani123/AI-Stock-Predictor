import type { Candle, Configuration, DashboardSummary, FuturesSnapshot, HealthResponse, HistoryResponse, IntelligenceHistoryResponse, IntelligenceResponse, LiveStatus, MemoryDayResponse, MemoryDaysResponse, MemorySummary, ModelsResponse, NewsEventsResponse, NewsStatusResponse, OptionsSnapshot, QualityResponse } from './types';

const BASE = import.meta.env.VITE_API_BASE_URL ?? '/api';
import type { MemoryCoverage, ScheduledEventResponse, EventIntelligence } from './types';
import type { EvidenceSummary, EvidenceJournalResponse, EvidenceRecord } from './types';
import type { SystemReadiness, EventImpactDetail, ContextV3Runtime, ContextV3ExperimentsResponse } from './types';
import type { MicrostructureRuntime, ShadowSummary, ShadowRecent } from './types';
export class ApiError extends Error { constructor(message: string, public readonly status: number) { super(message); } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`${BASE}${path}`, { headers: {'Accept': 'application/json'}, ...init }); }
  catch { throw new ApiError('Symfony backend is offline.', 0); }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(body.message ?? `Request failed (${response.status})`, response.status);
  return body as T;
}
export const api = {
  microstructureRuntime: () => request<MicrostructureRuntime>('/research/microstructure/runtime'),
  shadowSummary: () => request<ShadowSummary>('/research/shadow/summary'),
  shadowRecent: () => request<{items:ShadowRecent[]}>('/research/shadow/recent'),
  readiness: () => request<SystemReadiness>('/system/readiness'),
  contextV3Coverage: () => request<SystemReadiness['contextV3']>('/research/context-v3/coverage'),
  contextV3Runtime: () => request<ContextV3Runtime>('/research/context-v3/runtime'),
  contextV3Experiments: () => request<ContextV3ExperimentsResponse>('/research/context-v3/experiments'),
  contextV3Profiles: () => request<{profiles:Array<Record<string,unknown>>;newsChannel:string;trainingPipeline:string}>('/research/context-v3/profiles'),
  marketStructureCoverage: () => request<SystemReadiness['marketStructureContext']>('/research/market-structure/coverage'),
  marketStructureDay: (date:string) => request<Record<string,unknown>>(`/research/market-structure/day/${encodeURIComponent(date)}`),
  eventImpactSummary: () => request<Record<string,unknown>>('/research/event-impact/summary'),
  eventImpact: (id:string) => request<EventImpactDetail>(`/research/event-impact/${encodeURIComponent(id)}`),
  evidenceSummary: () => request<EvidenceSummary>('/research/evidence-summary'),
  evidenceJournal: (params = '') => request<EvidenceJournalResponse>(`/research/evidence-journal${params ? `?${params}` : ''}`),
  evidenceDetail: (id:string) => request<{available:boolean;record:EvidenceRecord}>(`/research/evidence-journal/${encodeURIComponent(id)}`),
  health: () => request<HealthResponse>('/health'), dashboard: () => request<DashboardSummary>('/dashboard'),
  candles: (limit = 500) => request<{candles: Candle[]}>(`/market/candles?instrument=BANKNIFTY&interval=1m&limit=${limit}`),
  predictions: (params = '') => request<HistoryResponse>(`/predictions/history${params ? `?${params}` : ''}`),
  live: () => request<LiveStatus>('/live/status'),
  startSimulation: () => request<LiveStatus>('/live/simulation/start', {method: 'POST'}),
  stopSimulation: () => request<LiveStatus>('/live/simulation/stop', {method: 'POST'}),
  options: (expiry = '') => request<OptionsSnapshot>(`/market/options${expiry ? `?expiry=${encodeURIComponent(expiry)}` : ''}`), futures: () => request<FuturesSnapshot>('/market/futures'),
  models: () => request<ModelsResponse>('/models/status'), quality: () => request<QualityResponse>('/data-quality'),
  configuration: () => request<Configuration>('/configuration'),
  intelligence: () => request<IntelligenceResponse>('/intelligence/current'),
  intelligenceHistory: (params = '') => request<IntelligenceHistoryResponse>(`/intelligence/history${params ? `?${params}` : ''}`),
  memorySimilarity: () => request<MemorySummary>('/memory/current-similarity'),
  memoryDays: () => request<MemoryDaysResponse>('/memory/days'),
  memoryCoverage: () => request<MemoryCoverage>('/memory/coverage'),
  newsScheduled: () => request<ScheduledEventResponse>('/news/scheduled'),
  memoryDay: (date: string) => request<MemoryDayResponse>(`/memory/day/${encodeURIComponent(date)}`),
  newsStatus: () => request<NewsStatusResponse>('/news/status'),
  newsEvents: (params = '') => request<NewsEventsResponse>(`/news/events${params ? `?${params}` : ''}`),
  newsEvent: (id: string) => request<Record<string, unknown>>(`/news/events/${encodeURIComponent(id)}`),
  newsEventMemory: (id: string) => request<EventIntelligence>(`/news/event-memory/${encodeURIComponent(id)}`),
  currentNewsMemory: () => request<Record<string, unknown>>('/news/event-memory/current'),
  newsRefresh: () => request<Record<string, unknown>>('/news/refresh', {method: 'POST'}),
};
