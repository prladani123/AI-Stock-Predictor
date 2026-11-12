import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, Panel, StatusBadge } from '../components/States';
import { useApi } from '../hooks';
import { MemoryCoveragePanel } from '../components/MemoryCoveragePanel';
import type { MemoryDayResponse } from '../types';

export function MarketMemoryPage() {
  const records = useApi(useCallback(() => api.memoryDays(), []));
  const [selected, setSelected] = useState<MemoryDayResponse | null>(null);
  const [raw, setRaw] = useState(false);
  const open = async (date: string) => { setSelected(await api.memoryDay(date)); setRaw(false); };
  useEffect(() => { const date = new URLSearchParams(location.search).get('date'); if (date) void open(date); }, []);
  if (records.loading && !records.data) return <LoadingState/>;
  if (records.error) return <ErrorState message={records.error} retry={records.refresh}/>;
  const day = selected?.day;
  return <div className="page"><PageTitle eyebrow="RESEARCHED HISTORICAL EVIDENCE" title="Market Memory V2" detail="50 curated BANKNIFTY sessions with graded causality and leakage-aware timing."/>
    <MemoryCoveragePanel/>
    <Panel title={`${records.data?.count ?? 0} VERIFIED DAILY RECORDS`}><div className="memory-table"><div className="memory-row memory-head"><b>Date</b><b>Direction</b><b>Evidence</b><b>Timing</b><b>Range</b><b>Quality</b></div>{records.data?.days.map(item => <button className="memory-row" key={item.date} onClick={() => void open(item.date)}><span>{item.date}</span><span>{item.direction}</span><span>{item.causalEvidenceGrade}</span><span>{item.timingPrecision.replaceAll('_', ' ')}</span><span>{item.range_percent?.toFixed(2) ?? '—'}%</span><StatusBadge label={item.sourceQuality} tone={item.sourceQuality === 'HIGH' ? 'good' : 'warn'}/></button>)}</div></Panel>
    {day && <Panel title={`${day.date} · ${day.direction}`} action={<button onClick={() => setRaw(!raw)}>{raw ? 'Structured view' : 'Original Markdown'}</button>}>{raw ? <pre className="memory-markdown">{selected?.markdown}</pre> : <div className="memory-detail"><div className="tag-row">{day.tags.map(tag => <span key={tag}>{tag}</span>)}</div><section><h3>What happened</h3><p>{day.whatHappened}</p></section><section><h3>Why it moved</h3><p>{day.whyItMoved}</p></section><section><h3>Pattern lesson</h3><p>{day.patternLesson}</p></section><section><h3>Sources</h3>{day.sourceMetadata.map((source, index) => <a key={source.url ?? index} href={source.url} target="_blank" rel="noreferrer">{source.title || source.url}</a>)}</section></div>}</Panel>}
  </div>;
}
