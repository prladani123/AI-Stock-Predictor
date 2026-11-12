import { useCallback } from 'react';
import { api } from '../api';
import { useApi } from '../hooks';
import { Panel } from './States';

export function MemoryCoveragePanel() {
  const result = useApi(useCallback(() => api.memoryCoverage(), []));
  const data = result.data;
  return <Panel title="MEMORY DATASET">{result.error ? <p role="alert">Coverage unavailable: {result.error}</p> : !data ? <p>Loading coverage…</p> : <>
    <div className="news-stats"><span>Daily memories <b>{data.dailyMemoryDays}</b></span><span>Complete minute-history days <b>{data.completeMinuteDays}</b></span><span>Numerical episodes <b>{data.numericalEpisodes}</b></span><span>Structured events <b>{data.structuredEvents}</b></span><span>Measurable reactions <b>{data.reactionMeasurableEvents}</b></span></div>
    <p>Historical intraday coverage: {data.completeMinuteDays} / {data.dailyMemoryDays} sessions</p>
    {data.missingMinuteDates.length > 0 && <p>Intraday enrichment pending for {data.missingMinuteDates.length} sessions</p>}
  </>}</Panel>;
}
