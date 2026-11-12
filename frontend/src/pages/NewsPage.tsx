import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, Panel, StatusBadge } from '../components/States';
import { useApi } from '../hooks';
import type { NewsEvent, EventIntelligence, EventImpactDetail } from '../types';
import { EventEvidence } from '../components/EventEvidence';

export function NewsPage() {
  const [category, setCategory] = useState('');
  const [relevance, setRelevance] = useState('');
  const [date, setDate] = useState('');
  const events = useApi(useCallback(() => {
    const params = new URLSearchParams();
    if (category) params.set('category', category);
    if (relevance) params.set('relevance', relevance);
    if (date) params.set('date', date);
    return api.newsEvents(params.toString());
  }, [category, relevance, date]));
  const status = useApi(useCallback(() => api.newsStatus(), []), 60000);
  const [selected, setSelected] = useState<NewsEvent | null>(null);
  const [memory, setMemory] = useState<EventIntelligence | null>(null);
  const [detailError, setDetailError] = useState('');
  const [impact, setImpact] = useState<EventImpactDetail | null>(null);
  const [impactError, setImpactError] = useState('');
  const requestId = useRef(0);
  const open = async (item: NewsEvent) => {
    const sequence=++requestId.current;
    setSelected(item);
    setMemory(null); setDetailError(''); setImpact(null); setImpactError('');
    const [memoryResult,impactResult]=await Promise.allSettled([api.newsEventMemory(item.event_id),api.eventImpact(item.event_id)]);
    if(sequence!==requestId.current) return;
    if(memoryResult.status==='fulfilled') {setMemory(memoryResult.value);setSelected(memoryResult.value.event ?? item);}
    else setDetailError(memoryResult.reason instanceof Error ? memoryResult.reason.message : 'Event evidence unavailable');
    if(impactResult.status==='fulfilled') setImpact(impactResult.value);
    else setImpactError('Impact study unavailable; existing event evidence is still shown.');
  };
  useEffect(() => {
    const id = new URLSearchParams(location.search).get('event');
    if (id && events.data) {
      const item = events.data.events.find(value => value.event_id === id);
      if (item) void open(item);
    }
  }, [events.data]);
  if (events.loading && !events.data) return <LoadingState/>;
  if (events.error) return <ErrorState message={events.error} retry={events.refresh}/>;
  return <div className="page">
    <PageTitle eyebrow="TIMESTAMPED SUPPORTING EVIDENCE" title="News & Event Intelligence"
      detail={`Monitoring window: ${status.data?.monitoringWindow?.replaceAll('_', ' ') ?? 'UNAVAILABLE'}`}
      actions={<button onClick={async () => { await api.newsRefresh(); await events.refresh(); await status.refresh(); }}>Refresh sources</button>}/>
    <Panel title="EVENT MEMORY" action={<div className="filters">
      <input placeholder="Category" value={category} onChange={event => setCategory(event.target.value.toUpperCase())}/>
      <input type="date" aria-label="Event date" value={date} onChange={event => setDate(event.target.value)}/>
      <select value={relevance} onChange={event => setRelevance(event.target.value)}><option value="">All relevance</option><option>HIGH</option><option>MEDIUM</option></select>
    </div>}>
      <div className="event-list">{events.data?.events.map(item => <button key={item.event_id} onClick={() => void open(item)}>
        <div><StatusBadge label={item.relevance} tone={item.relevance === 'HIGH' ? 'bad' : 'warn'}/><b>{item.event_category.replaceAll('_', ' ')}</b><small>{item.first_seen_at ? new Date(item.first_seen_at).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata'}) : item.knowledge_phase}</small></div>
        <p>{item.headline_or_summary}</p><span>{item.predictive_eligible ? 'PREDICTIVE ELIGIBLE' : 'CONTEXT ONLY'}</span>
      </button>)}</div>
    </Panel>
    <Panel title={`NEWS MONITOR · ${status.data?.monitor?.state ?? 'UNAVAILABLE'}`}>
      <p>Last successful refresh: {status.data?.monitor?.lastSuccessfulRefresh ?? 'Not yet recorded'}</p>
      {(status.error || status.data?.monitor?.lastError) && <p role="alert">{status.error || status.data?.monitor?.lastError}</p>}
      <div className="news-stats">{status.data?.sources.map(source => <div key={source.source}><b>{source.source} · {source.state}</b><p>Last attempt: {source.checked_at}</p><p>Last success: {'last_success_at' in source ? String(source.last_success_at ?? 'Not recorded') : 'Not recorded'}</p><p>{source.message || 'No reported error'}</p></div>)}</div>
    </Panel>
    {selected && <Panel title="EVENT INTELLIGENCE">{detailError && <p role="alert">{detailError}</p>}{impactError && <p role="alert">{impactError}</p>}<EventEvidence event={selected} memory={memory} impact={impact}/></Panel>}
  </div>;
}
