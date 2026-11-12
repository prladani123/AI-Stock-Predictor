import { Fragment, useCallback } from 'react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, Panel, StatusBadge } from '../components/States';
import { formatIst, useApi } from '../hooks';
import type { ReadinessState } from '../types';

const title = (key: string) => key.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/_/g, ' ');
function Value({value}: {value: unknown}) {
  if (value == null) return <span>Not available</span>;
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>;
  if (Array.isArray(value)) return value.length ? <ul>{value.map((item, index) => <li key={index}><Value value={item}/></li>)}</ul> : <span>None</span>;
  if (typeof value === 'object') return <dl className="detail-grid">{Object.entries(value).map(([key, item]) => <Fragment key={key}><dt>{title(key)}</dt><dd><Value value={item}/></dd></Fragment>)}</dl>;
  return <span>{String(value)}</span>;
}
function Badge({state}: {state: ReadinessState}) {
  return <StatusBadge label={state} tone={state === 'READY' ? 'good' : state === 'BLOCKED' ? 'bad' : state === 'PARTIAL' ? 'warn' : 'neutral'}/>;
}
const runtimeTone = (status: string): 'good'|'bad'|'warn'|'neutral' =>
  status === 'READY' || status === 'PASS' ? 'good' : status === 'PARTIAL' ? 'warn' : status === 'UNAVAILABLE' ? 'bad' : 'neutral';
const experimentTone = (status: string): 'good'|'bad'|'warn'|'neutral' =>
  status === 'V3_BETTER' ? 'good' : status === 'V3_WORSE' ? 'bad' : status.startsWith('INSUFFICIENT') ? 'warn' : 'neutral';
export function SettingsPage() {
  const state = useApi(useCallback(() => api.readiness(), []), 60000);
  const configuration = useApi(useCallback(() => api.configuration(), []), 60000);
  const runtime = useApi(useCallback(() => api.contextV3Runtime(), []), 30000);
  const experiments = useApi(useCallback(() => api.contextV3Experiments(), []), 60000);
  const microstructure = useApi(useCallback(() => api.microstructureRuntime(), []), 30000);
  const shadow = useApi(useCallback(() => api.shadowSummary(), []), 60000);
  const data = state.data;
  return <div className="page"><PageTitle eyebrow="READ-ONLY OPERATIONAL STATE" title="System Readiness"
    detail="Stored evidence and actual runtime state. This page does not fetch market data, train or approve models."
    actions={<button onClick={() => { void state.refresh(); void configuration.refresh(); void runtime.refresh(); void experiments.refresh(); }}>Refresh readiness</button>}/>
    {state.loading && !data ? <LoadingState/> : state.error ? <ErrorState message={state.error} retry={state.refresh}/> : null}
    {data && <>
      <p>Observed {formatIst(data.observedAt)}. Historical V2 models are price-core unless their actual training schema includes derivatives.</p>
      <Panel title="Actionable blockers">{data.blockers.length ? <ul>{data.blockers.map(blocker => <li key={blocker}>{title(blocker)}</li>)}</ul> : <p>No blockers detected in this snapshot.</p>}</Panel>
      {data.researchReadiness && <Panel title="RESEARCH IMPLEMENTATION / DATA / EVIDENCE"><Value value={data.researchReadiness}/></Panel>}
      <Panel title="APPLICATION" action={<Badge state={data.categories.APPLICATION}/>}><Value value={data.application}/></Panel>
      <Panel title="MARKET DATA" action={<Badge state={data.categories.MARKET_DATA}/>}><Value value={{calendar: data.calendar, zerodha: data.zerodha, instrumentSnapshot: data.instrumentSnapshot, liveWorker: data.live}}/></Panel>
      <Panel title="MARKET MEMORY" action={<Badge state={data.categories.MARKET_MEMORY}/>}><Value value={data.marketMemory}/></Panel>
      <Panel title="BIG MOVE V2 — RESEARCH" action={<Badge state={data.categories.MODEL_RESEARCH}/>}><Value value={data.research}/></Panel>
      <Panel title="BIG MOVE V2 — MODELS & RUNTIME" action={<Badge state={data.categories.MODELS}/>}><Value value={{models: data.models, runtime: data.runtime}}/></Panel>
      <Panel title="NEWS" action={<Badge state={data.categories.NEWS}/>}><Value value={data.news}/></Panel>
      <Panel title="SCHEDULED EVENT CONFIGURATION" action={<StatusBadge label={data.news.scheduledContext?.status ?? 'NOT_CONFIGURED'} tone={data.news.scheduledContext?.status==='CONFIGURED'?'good':data.news.scheduledContext?.status==='INVALID'?'bad':'neutral'}/>}>
        <Value value={data.news.scheduledContext}/>
        <p>Configuration status is independent of whether events are still upcoming. DATE_ONLY events expose no invented release time.</p>
      </Panel>
      <Panel title="BANKNIFTY CONSTITUENTS" action={<Badge state={data.categories.CONSTITUENT_CONTEXT}/>}>
        <Value value={data.marketStructureContext.CONSTITUENT_CONTEXT}/>
        <p>Tracking membership is date-scoped; historical membership is never inferred from today's list.</p>
        <details><summary>Market structure and constituent news</summary><Value value={data.marketStructureContext.MARKET_STRUCTURE}/><Value value={data.marketStructureContext.UPSTOX_INSTRUMENT_NEWS}/></details>
      </Panel>
      <Panel title="HISTORICAL FUTURES" action={<Badge state={data.categories.FUTURES_CONTEXT}/>}><Value value={data.marketStructureContext.FUTURES_CONTEXT}/><p>Expired futures require Upstox Plus. Missing access does not disable other data planes.</p></Panel>
      <Panel title="EVENT IMPACT STUDIES" action={<Badge state={data.categories.EVENT_IMPACT}/>}><Value value={data.eventImpact}/><p>Historical reaction priors are descriptive evidence, not prediction probabilities.</p></Panel>
      <Panel title="CONTEXT V3" action={<StatusBadge label={data.contextV3.status} tone={data.contextV3.status==='READY_FOR_EVALUATION'?'good':data.contextV3.status==='PARTIAL_COVERAGE'?'warn':'neutral'}/>}>
        <p>PRICE + VOL · INDIA MARKET · GLOBAL · DERIVATIVES · BANK STRUCTURE · SESSION STRUCTURE</p>
        <p>Independent profiles; missing inputs remain unavailable. Comparison requires identical V2 dates, reference minutes and labels. V2 model pipelines are unchanged. Stage G3 evaluation framework is implemented; Context V3 superiority is unproven until stored experiments support it.</p>
        <details open>
          <summary><strong>Runtime Context V3</strong>{runtime.data && <> <StatusBadge label={runtime.data.status} tone={runtimeTone(runtime.data.status)}/></>}</summary>
          {runtime.loading && !runtime.data ? <LoadingState/> : runtime.error ? <p>Runtime Context V3 unavailable: {runtime.error}</p> : runtime.data && <>
            <p>As of {formatIst(runtime.data.asOf)} · market as of {formatIst(runtime.data.marketAsOf)}</p>
            {runtime.data.reasons.length > 0 && <p>{runtime.data.reasons.join(' · ')}</p>}
            <dl className="detail-grid">
              {Object.values(runtime.data.profiles).map(profile=><Fragment key={profile.profileVersion}>
                <dt>{profile.profileVersion}</dt>
                <dd>
                  <StatusBadge label={profile.status} tone={runtimeTone(profile.status)}/>
                  <div>10m {profile.horizonEligibility['10'] ? 'eligible' : 'ineligible'} · 20m {profile.horizonEligibility['20'] ? 'eligible' : 'ineligible'} · 30m {profile.horizonEligibility['30'] ? 'eligible' : 'ineligible'}</div>
                  <div>{profile.requiredBlocks.map(name => `${name} ${profile.blockStatuses[name] ?? 'UNAVAILABLE'}`).join(' · ')}</div>
                  {profile.missingReasons.length > 0 && <small>{profile.missingReasons.join(' · ')}</small>}
                </dd>
              </Fragment>)}
            </dl>
          </>}
        </details>
        {data.contextV3.profiles?.map(profile=><details key={profile.profileVersion}><summary>{profile.profileVersion} — {profile.status}</summary>
          {profile.cohorts.length ? profile.cohorts.map(cohort=><div key={`${cohort.datasetRevisionId}:${cohort.instrumentId}`}>
            <p>{cohort.passRows} / {cohort.totalCandidateRows} PASS ({cohort.coveragePercent.toFixed(1)}%) · {cohort.tradingDayCount} days · lineage {cohort.pinnedLineageAvailable?'pinned':'unavailable'}</p>
            <Value value={{readyDates:cohort.readyDates,missingDatesBySource:cohort.missingDatesBySource,ambiguousDates:cohort.ambiguousDates,minimumCoverage:cohort.minimumCoverage,commonSupport:cohort.commonSupportWithPriceCoreV2,missingBlocks:cohort.missingBlockCounts,sourceConflicts:cohort.sourceConflictRows,CASExclusions:cohort.casBoundaryExclusions,indexCloseExclusions:cohort.indexCloseExclusions}}/>
          </div>) : <p>No stored historical feature rows.</p>}
        </details>)}
      </Panel>
      <Panel title="CONTEXT V3 EXPERIMENTS" action={<StatusBadge label={experiments.data ? `${experiments.data.count} STORED` : 'LOADING'} tone={experiments.error ? 'bad' : 'neutral'}/>}>
        <p>Paired chronological evidence only. Candidate rows must share the exact V2 reference minute, horizon and label. No experiment approves or changes a production model.</p>
        {experiments.loading && !experiments.data ? <LoadingState/> : experiments.error ? <p>Experiment evidence unavailable: {experiments.error}</p> : experiments.data && <>
          {experiments.data.notice && <p>{experiments.data.notice}</p>}
          {experiments.data.experiments.length === 0 ? <p>No stored Context V3 experiments.</p> : experiments.data.experiments.map(item => <details key={item.version}>
            <summary>{item.profileVersion ?? 'Context V3'} — <StatusBadge label={item.classification ?? 'UNKNOWN'} tone={experimentTone(item.classification ?? 'UNKNOWN')}/></summary>
            <p>{item.dateFrom ?? 'unbounded'} to {item.dateTo ?? 'unbounded'} · revision {item.datasetRevisionId ?? 'unknown'} · instrument {item.instrumentId ?? 'unknown'}</p>
            <dl className="detail-grid">
              {Object.entries(item.horizons).map(([horizon, row]) => <Fragment key={horizon}>
                <dt>{horizon} minutes</dt><dd><StatusBadge label={row.classification ?? row.status} tone={experimentTone(row.classification ?? row.status)}/>
                  {row.evaluation && <span> · {row.evaluation.rows} paired rows · {row.evaluation.dates} days</span>}</dd>
              </Fragment>)}
            </dl>
            {item.blockers.length > 0 && <p>{item.blockers.join(' · ')}</p>}
          </details>)}
        </>}
      </Panel>
      <Panel title="EVIDENCE" action={<Badge state={data.categories.EVIDENCE}/>}><Value value={data.evidence}/></Panel>
      <Panel title="RESEARCH CONTEXT" action={<Badge state={data.categories.RESEARCH_CONTEXT}/> }>
        <p>Optional historical research providers; Zerodha remains the live feed. Counts are sessions with observations, not complete minute histories.</p>
        <Value value={data.researchContext.providers}/>
        {data.researchContext.coverage && <dl className="detail-grid">
          {Object.entries({'Options history': data.researchContext.coverage.sessionsWithOptionContext,
            'NIFTY 50':data.researchContext.coverage.sessionsWithNifty50,
            'India VIX': data.researchContext.coverage.sessionsWithVix, 'GIFT NIFTY': data.researchContext.coverage.sessionsWithGiftNifty,
            'Brent': data.researchContext.coverage.sessionsWithBrent, 'USD/INR': data.researchContext.coverage.sessionsWithUsdInr,
            'Historical PCR': data.researchContext.coverage.sessionsWithHistoricalPCR, 'Historical OI': data.researchContext.coverage.sessionsWithHistoricalOI,
            'Historical Max Pain': data.researchContext.coverage.sessionsWithHistoricalMaxPain}).map(([name, count]) =>
              <Fragment key={name}><dt>{name}</dt><dd>{['UNAVAILABLE','NOT_CONFIGURED'].includes(data.researchContext.coverage?.status ?? '') ? 'Not available' : `${count} / ${data.researchContext.coverage?.canonicalSessions}`}</dd></Fragment>)}
        </dl>}
        <Value value={data.researchContext.nextRequiredAction}/>
        <details><summary>Last research requests</summary><Value value={data.researchContext.lastRequestStates}/></details>
      </Panel>
    </>}
    <Panel title="MICROSTRUCTURE — RESEARCH ONLY">
      <p>NOT USED BY PRICE_CORE_V2. Missing depth remains unavailable; no estimated Greeks.</p>
      {microstructure.error ? <ErrorState message={microstructure.error} retry={microstructure.refresh}/> : <Value value={microstructure.data}/>}
    </Panel>
    <Panel title="PROSPECTIVE SHADOW RESEARCH">
      <p>NO PRODUCTION PROBABILITY MODIFICATION. Descriptive, overlapping evidence cohorts only.</p>
      {shadow.error ? <ErrorState message={shadow.error} retry={shadow.refresh}/> : <Value value={shadow.data}/>}
    </Panel>
    <details className="technical"><summary>Runtime configuration</summary><Panel>
      {configuration.error ? <ErrorState message={configuration.error} retry={configuration.refresh}/> : configuration.data ? <Value value={configuration.data}/> : <LoadingState/>}
    </Panel></details>
  </div>;
}
