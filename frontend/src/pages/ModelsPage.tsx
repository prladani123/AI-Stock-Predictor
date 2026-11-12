import { useCallback } from 'react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, Panel } from '../components/States';
import { useApi } from '../hooks';

const percent = (value: number | null | undefined) => value == null ? 'Unavailable' : `${(value * 100).toFixed(1)}%`;
const decimal = (value: number | null | undefined) => value == null ? 'Unavailable' : value.toFixed(4);

export function ModelsPage() {
  const state = useApi(useCallback(() => api.models(), []));
  const data = state.data;
  return <div className="page">
    <PageTitle eyebrow="STAGE D / MODEL EVIDENCE" title="Big Move V2 Models" detail="Chronological evidence, calibration and immutable approval for 10 / 20 / 30 minutes." />
    {state.loading ? <LoadingState /> : state.error ? <ErrorState message={state.error} retry={state.refresh} /> : data && <>
      <p>{data.disclaimer}</p>
      {data.reason && <p role="status">{data.reason} — forecasts remain DATA_NOT_READY without approved artifacts and ready inputs.</p>}
      <div className="model-grid">{data.models.map(model => <Panel key={model.horizonMinutes} title={`${model.horizonMinutes} MIN`}>
        <dl className="detail-grid">
          <dt>Status</dt><dd>{model.status}</dd>
          <dt>Why not live</dt><dd>{model.reason ?? 'Evidence approved; live input readiness is checked separately.'}</dd>
          <dt>Algorithm</dt><dd>{model.algorithm ?? 'Not trained'}</dd>
          <dt>Version</dt><dd>{model.version ?? 'Unavailable'}</dd>
          <dt>Training revision</dt><dd>{model.trainingRevision ?? 'Unavailable'}</dd>
          <dt>Training rows / features</dt><dd>{model.trainingRows ?? '—'} / {model.featureCount ?? '—'}</dd>
          <dt>Move precision</dt><dd>{percent(model.metrics?.stageA.precision)}</dd>
          <dt>Move recall</dt><dd>{percent(model.metrics?.stageA.recall)}</dd>
          <dt>Move F1</dt><dd>{decimal(model.metrics?.stageA.f1)}</dd>
          <dt>Move Brier</dt><dd>{decimal(model.metrics?.stageA.brier)}</dd>
          <dt>Direction accuracy</dt><dd>{percent(model.metrics?.stageB.accuracy)}</dd>
          <dt>Direction samples</dt><dd>{model.metrics?.stageB.sampleCount ?? 'Unavailable'}</dd>
          <dt>Data-ready coverage</dt><dd>{percent(model.metrics?.coverage)}</dd>
          <dt>Calibration</dt><dd>{model.calibration ? Object.entries(model.calibration).map(([stage, value]) => `${stage}: ${value.method ?? value.status}`).join(' / ') : 'UNAVAILABLE'}</dd>
          <dt>Move threshold</dt><dd>{percent(model.decisionThreshold)}</dd>
          <dt>Approval</dt><dd>{model.approvalResult}</dd>
          <dt>Exact candidate holdout</dt><dd>{model.metrics?.exactCandidateHoldout?.scoredRows ?? 'Unavailable'} predictions</dd>
          <dt>Policy</dt><dd>{model.policyVersion ?? 'Not evaluated'}</dd>
        </dl>
        {model.approvalReasons.length > 0 && <ul>{model.approvalReasons.map(reason => <li key={reason}>{reason}</li>)}</ul>}
        <details><summary>Immutable lineage</summary><dl className="detail-grid">
          <dt>Model checksum</dt><dd>{model.modelChecksum ?? 'Unavailable'}</dd>
          <dt>Artifact checksum</dt><dd>{model.artifactChecksum ?? 'Unavailable'}</dd>
          <dt>Evaluation checksum</dt><dd>{model.evaluationChecksum ?? 'Not evaluated'}</dd>
          <dt>Approval checksum</dt><dd>{model.approvalChecksum ?? 'Not approved'}</dd>
        </dl></details>
      </Panel>)}</div>
      <Panel title="Candidate comparison — Logistic vs LightGBM">
        {!data.comparisons.length ? <p>No chronological comparison evidence is stored.</p> : data.comparisons.map(comparison => <section key={comparison.horizonMinutes}>
          <h3>{comparison.horizonMinutes} MIN — {comparison.selectedAlgorithm ?? 'INSUFFICIENT_EVIDENCE'}</h3>
          <p>{comparison.selectionReason.reason}</p>
          <div className="table-scroll"><table><thead><tr><th>Candidate</th><th>Move precision</th><th>Recall</th><th>Brier</th><th>Direction accuracy</th><th>Macro F1</th><th>Coverage</th></tr></thead>
            <tbody>{Object.entries(comparison.candidates).map(([algorithm, candidate]) => <tr key={algorithm}>
              <td>{algorithm}</td><td>{percent(candidate.metrics.movePrecision)}</td><td>{percent(candidate.metrics.moveRecall)}</td>
              <td>{decimal(candidate.metrics.brier)}</td><td>{percent(candidate.metrics.directionAccuracy)}</td>
              <td>{decimal(candidate.metrics.macroF1)}</td><td>{percent(candidate.metrics.coverage)}</td>
            </tr>)}</tbody></table></div>
          <details><summary>Selection rule and differences</summary><p>{comparison.selectionReason.rule}</p>
            <p>Rejected: {comparison.selectionReason.rejectedAlgorithm ?? 'No selection'}</p>
            <dl className="detail-grid">{Object.entries(comparison.selectionReason.metricDifferences).map(([name, value]) =>
              <div key={name}><dt>{name}</dt><dd>{decimal(value)}</dd></div>)}</dl>
          </details>
        </section>)}
      </Panel>
      <details><summary>Legacy Models — 5 / 10 / 15 / 20 minutes</summary>
        {!data.legacyModels.length ? <p>No legacy artifacts registered.</p> : data.legacyModels.map(model => <Panel key={`${model.horizonMinutes}-${model.version}`} title={`${model.horizonMinutes} MIN / ${model.algorithm}`}>
          <p>{model.version} — {model.status}</p><p>Calibration: {model.calibration}</p>
        </Panel>)}
      </details>
    </>}
  </div>;
}
