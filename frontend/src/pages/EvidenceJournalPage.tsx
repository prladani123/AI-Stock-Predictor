import { useCallback, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { EmptyState, ErrorState, LoadingState, Panel } from '../components/States';
import { formatIst, useApi } from '../hooks';
import type { EvidenceMetrics } from '../types';

const rate = (value:number|null|undefined) => value == null ? 'Unavailable' : `${(value*100).toFixed(1)}%`;
const difference = (value:number|null) => value == null ? 'Not reported' : `${value>=0?'+':''}${(value*100).toFixed(1)} percentage points (descriptive only)`;

function Cohort({title, data}:{title:string;data:EvidenceMetrics}) {
  return <section><h3>{title}</h3>
    <p>{data.status === 'INSUFFICIENT_SAMPLE' ? `INSUFFICIENT SAMPLE — ${data.samples} observations` : `DESCRIPTIVE ONLY — ${data.samples} observations`}</p>
    <dl className="detail-grid">
      <dt>Move precision</dt><dd>{rate(data.movePrecision)}</dd>
      <dt>Move recall</dt><dd>{rate(data.moveRecall)}</dd>
      <dt>Direction accuracy</dt><dd>{rate(data.directionAccuracy)} / {data.directionSamples} actual moves</dd>
      <dt>Actual big-move rate</dt><dd>{rate(data.actualBigMoveRate)}</dd>
      <dt>Precision difference vs baseline</dt><dd>{difference(data.movePrecisionDifference)}</dd>
      <dt>Direction difference vs baseline</dt><dd>{difference(data.directionAccuracyDifference)}</dd>
    </dl>
  </section>;
}

function Frozen({title, value}:{title:string;value:unknown}) {
  return <details><summary>{title}</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{JSON.stringify(value,null,2) ?? 'Unavailable'}</pre></details>;
}

function EvidenceDetail({id}:{id:string}) {
  const state=useApi(useCallback(()=>api.evidenceDetail(id),[id]));
  const record=state.data?.record;
  return <Panel title="Frozen decision evidence">
    <p><Link to="/evidence">Close detail</Link></p>
    <p>This is the evidence stored at prediction creation, not today's dashboard context. Predictions made before journal capture have no reconstructed evidence.</p>
    {state.loading ? <LoadingState/> : state.error ? <ErrorState message={state.error} retry={state.refresh}/> : record && <>
      <dl className="detail-grid">
        <dt>DECISION AT</dt><dd>{formatIst(record.decision_at)}</dd>
        <dt>MARKET DATA THROUGH</dt><dd>{formatIst(record.market_as_of)}</dd>
        <dt>NEWS KNOWN THROUGH</dt><dd>{formatIst(String(record.source_watermarks.newsFirstSeenCutoff))}</dd>
        <dt>Horizon</dt><dd>{record.horizon_minutes} MIN</dd>
        <dt>Actual result</dt><dd>{record.resolved ? `${record.actual_label} / ${record.outcome_quality}` : 'Pending'}</dd>
      </dl>
      <Frozen title="Prediction — exact baseline probabilities" value={record.baseline_prediction}/>
      <Frozen title="Model and journal lineage" value={{modelVersion:record.model_version,moveModel:record.move_model_checksum,
        directionModel:record.direction_model_checksum,evaluation:record.evaluation_checksum,approval:record.approval_checksum,
        approvalPolicy:record.approval_policy_version,journal:record.journal_id,checksum:record.journal_checksum,
        thresholdPolicy:record.market_evidence.thresholdPolicy,thresholdUsed:record.market_evidence.thresholdUsed}}/>
      <Frozen title="Price evidence" value={record.market_evidence.PRICE}/>
      <Frozen title="Futures evidence" value={record.market_evidence.FUTURES}/>
      <Frozen title="Options evidence" value={record.market_evidence.OPTIONS}/>
      <Frozen title="Regime and session bias" value={record.market_evidence.REGIME}/>
      <Frozen title="Numerical memory AT THAT TIME" value={record.numerical_memory}/>
      <Frozen title="News/event memory and market confirmation AT THAT TIME" value={record.news_memory}/>
      <Frozen title="Descriptive evidence alignment" value={record.evidence_alignment}/>
      <Frozen title="Source cutoffs" value={record.source_watermarks}/>
      <Frozen title="Full frozen feature values" value={record.market_evidence.fullFeatureValues}/>
      <Frozen title="Actual outcome — joined, never written into the journal" value={{attachedAt:record.attached_at,quality:record.outcome_quality,result:record.realized_summary}}/>
    </>}
  </Panel>;
}

export function EvidenceJournalPage() {
  const [params]=useSearchParams();
  const selected=params.get('prediction_id');
  const [page,setPage]=useState(1), [date,setDate]=useState(''), [horizon,setHorizon]=useState(''), [resolved,setResolved]=useState('');
  const query=useMemo(()=>new URLSearchParams({page:String(page),pageSize:'25',...(date&&{date}),...(horizon&&{horizon}),...(resolved&&{resolved})}).toString(),[page,date,horizon,resolved]);
  const summary=useApi(useCallback(()=>api.evidenceSummary(),[]));
  const journal=useApi(useCallback(()=>api.evidenceJournal(query),[query]));
  const filter=(setter:(v:string)=>void,value:string)=>{setter(value);setPage(1);};
  return <div className="page">
    <PageTitle eyebrow="PROSPECTIVE EVIDENCE" title="Evidence Journal" detail="Immutable decision-time research; live dashboard evidence remains separate."
      actions={<button onClick={()=>{void summary.refresh();void journal.refresh();}}>Refresh</button>}/>
    {selected && <EvidenceDetail key={selected} id={selected}/>}
    {summary.loading ? <LoadingState/> : summary.error ? <ErrorState message={summary.error} retry={summary.refresh}/> : summary.data && <>
      <p>Resolved observations: {summary.data.resolvedPredictions}. Minimum cohort and metric-denominator sample: {summary.data.minimumSample}.</p>
      <p>{summary.data.notice}</p>
      <div className="model-grid">{summary.data.horizons.map(h=><Panel key={h.minutes} title={`${h.minutes} MIN`}>
        <Cohort title="BASE MODEL" data={h.baseline}/>
        <p>Predicted big moves: {h.baseline.predictedBigMoveCount}; actual big moves: {h.baseline.actualBigMoveCount}. Quiet accuracy: {rate(h.baseline.noBigMoveAccuracy)}. Scorable coverage: {rate(h.baseline.coverage)}.</p>
        <Cohort title="WHEN NUMERICAL MEMORY AGREED" data={h.numericalMemory.SUPPORTS_MODEL}/>
        <Cohort title="WHEN NEWS AGREED" data={h.newsMemory.SUPPORTS_MODEL}/>
        <Cohort title="WHEN BOTH AGREED" data={h.combined.MODEL_PLUS_MEMORY_AND_NEWS}/>
        <Cohort title="WHEN EVIDENCE CONTRADICTED MODEL" data={h.contradicted}/>
        <details><summary>All memory, news and confirmation cohorts</summary>
          {Object.entries({numericalMemory:h.numericalMemory,newsMemory:h.newsMemory,confirmation:h.confirmation,combined:h.combined}).map(([channel,cohorts])=>
            <section key={channel}><h3>{channel}</h3>{Object.entries(cohorts).map(([name,data])=><Cohort key={name} title={name} data={data}/>)}</section>)}
        </details>
      </Panel>)}</div>
      <Frozen title="Research metric definitions and limitations" value={summary.data.definitions}/>
    </>}
    <Panel title="Prospective journal records">
      <p>Summary above covers all stored resolved observations. These filters apply only to the journal table.</p>
      <div className="filters">
        <input aria-label="Market date (IST)" type="date" value={date} onChange={e=>filter(setDate,e.target.value)}/>
        <select aria-label="Horizon" value={horizon} onChange={e=>filter(setHorizon,e.target.value)}><option value="">All horizons</option>{[10,20,30].map(h=><option key={h} value={h}>{h} minutes</option>)}</select>
        <select aria-label="Resolution" value={resolved} onChange={e=>filter(setResolved,e.target.value)}><option value="">All outcomes</option><option value="true">Resolved</option><option value="false">Pending</option></select>
      </div>
      {journal.loading ? <LoadingState/> : journal.error ? <ErrorState message={journal.error} retry={journal.refresh}/> : !journal.data?.items.length ?
        <EmptyState title="No prospective evidence" detail="Only new real predictions captured atomically with their evidence appear here. Old predictions are not backfilled."/> : <>
        <div className="table-scroll"><table><thead><tr><th>TIME (DECISION)</th><th>HORIZON</th><th>MODEL</th><th>BIG MOVE %</th><th>DIRECTION</th><th>NUMERICAL MEMORY</th><th>NEWS</th><th>CONFIRMATION</th><th>ACTUAL</th><th>STATUS</th></tr></thead>
          <tbody>{journal.data.items.map(r=><tr key={r.journal_id}>
            <td><Link to={`/evidence?prediction_id=${encodeURIComponent(r.prediction_id)}`}>{formatIst(r.decision_at)}</Link></td>
            <td>{r.horizon_minutes}m</td><td>{r.model_version}</td><td>{rate(r.baseline_prediction.bigMoveProbability)}</td><td>{r.baseline_prediction.direction ?? 'NO_BIG_MOVE'}</td>
            <td>{r.evidence_alignment.numericalMemory}</td><td>{r.evidence_alignment.newsMemory}</td><td>{r.evidence_alignment.confirmationState}</td>
            <td>{r.actual_label ?? 'Pending'}</td><td>{r.resolved ? r.outcome_quality === 'VALID' ? 'RESOLVED' : 'EXCLUDED_OUTCOME' : 'PENDING'}</td>
          </tr>)}</tbody></table></div>
        <div className="pagination"><button disabled={page<=1} onClick={()=>setPage(page-1)}>Previous</button><span>Page {page} / {Math.max(1,Math.ceil(journal.data.total/journal.data.pageSize))}</span><button disabled={page*journal.data.pageSize>=journal.data.total} onClick={()=>setPage(page+1)}>Next</button></div>
      </>}
    </Panel>
  </div>;
}
