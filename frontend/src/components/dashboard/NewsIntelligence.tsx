import { Newspaper, RefreshCw } from 'lucide-react';
import { useCallback } from 'react';
import { useApi } from '../../hooks';
import { api } from '../../api';
import { Panel, StatusBadge } from '../States';
import type { NewsSummary } from '../../types';

const pct = (value?: number | null) => value == null ? '—' : `${Math.round(value * 100)}%`;

export function NewsIntelligence({news, refresh}: {news?: NewsSummary; refresh: () => void}) {
  const event = news?.latestHighImpactEvent;
  const health = useApi(useCallback(() => api.newsStatus(), []), 60000);
  const scheduled = useApi(useCallback(() => api.newsScheduled(), []), 60000);
  const upcoming = scheduled.data?.events[0];
  return <Panel title="NEWS / EVENT INTELLIGENCE" action={<button onClick={refresh}><RefreshCw size={14}/>Refresh news</button>}><div className="news-intelligence">
    <div className="news-monitor"><Newspaper size={20}/><div><small>MONITORING WINDOW</small><b>{news?.monitoringWindow?.replaceAll('_', ' ') ?? 'UNAVAILABLE'}</b></div></div>
    <div><small>NEWS MONITOR</small><b> {health.error ? 'UNAVAILABLE' : health.data?.monitor?.state ?? 'LOADING'}</b><p>Last successful refresh: {health.data?.monitor?.lastSuccessfulRefresh ? new Date(health.data.monitor.lastSuccessfulRefresh).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata'})+' IST' : 'Not yet recorded'}</p></div>
    <div><small>UPCOMING KNOWN EVENT</small>{upcoming ? <p><a href={upcoming.sourceUrl} target="_blank" rel="noreferrer">{upcoming.title}</a> · {upcoming.expectedReleaseTimeKnown && upcoming.scheduledAt ? `${new Date(upcoming.scheduledAt).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})} IST` : `${upcoming.scheduledDate} — release time unknown`} · {upcoming.marketRelevance} relevance</p> : <p className="muted">{scheduled.error ? 'Schedule unavailable' : 'NOT CONFIGURED — no upcoming event configured'}</p>}</div>
    {!news?.available || !event ? <p className="muted">No high-relevance live event is currently stored. Big Move V2 remains operational.</p> : <>
      <header><div><StatusBadge label={event.category.replaceAll('_', ' ')} tone="warn"/><small>First seen {new Date(event.firstSeenAt).toLocaleTimeString('en-IN', {hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata'})} IST · {event.source}</small></div><StatusBadge label={event.relevance} tone="bad"/></header>
      <h3>{event.headline}</h3><div className="news-stats"><span>Historical matches <b>{news.historicalMatches ?? 0}</b></span><span>Historical 30m move <b>{pct(news.historicalBigMove30Rate)}</b></span><span>Direction <b>{news.historicalDirectionBias ?? '—'} {pct(news.historicalDirectionRate)}</b></span><span>Confirmation <b>{news.marketConfirmation?.replaceAll('_', ' ') ?? 'INSUFFICIENT DATA'}</b></span></div><a className="text-link" href={`/news?event=${event.eventId}`}>View event evidence</a>
    </>}
  </div></Panel>;
}
