import { Panel, StatusBadge } from '../States';
import { formatNumber } from '../../hooks';
import type { ExperimentalForecast, ExternalResearchHorizon, IntelligenceResponse } from '../../types';

function ResearchModel({title, forecast}: {title: string; forecast: ExperimentalForecast | undefined}) {
  const summary = forecast?.summary ?? {};
  const horizons = (forecast?.horizons ?? summary.horizons ?? {}) as Record<string, ExternalResearchHorizon>;
  const ordered = ['10', '20', '30']
    .map(key => horizons[key])
    .filter((item): item is ExternalResearchHorizon => Boolean(item));
  const latency = Number(summary.kronosLatencyMs ?? 0);
  const priorSessions = Number(summary.priorSessionCount ?? 0);
  const pointInTime = String(forecast?.pointInTimeStatus ?? '');
  const postDecision = pointInTime === 'PROSPECTIVE_POST_DECISION';
  return <div className="external-model">
    <header>
      <div><span>{title}</span><small>{String(forecast?.model ?? '')}</small></div>
      <StatusBadge label={postDecision ? 'POST_DECISION' : String(forecast?.status ?? 'UNAVAILABLE')}
        tone={postDecision ? 'warn' : forecast?.status === 'COMPLETED' ? 'good' : forecast?.status === 'SERVICE_UNAVAILABLE' ? 'warn' : 'neutral'}/>
    </header>
    {!forecast?.available ? <p className="external-empty">{String(forecast?.reason ?? 'No research output')}</p> :
      <>
        <div className="external-horizons">{ordered.map(item => <div key={item.minutes}>
          <small>{item.minutes} MIN</small>
          <strong>{formatNumber(item.pMeaningfulMove * 100, 1)}% meaningful</strong>
          <span>UP {formatNumber(item.pUpGivenMeaningfulMove * 100, 1)}% / DOWN {formatNumber(item.pDownGivenMeaningfulMove * 100, 1)}%</span>
          <span>E[return] {formatNumber(item.expectedReturn * 100, 2)}% · E[move] {formatNumber(item.expectedMagnitude * 100, 2)}%</span>
          <span>p05 {formatNumber(item.p05 * 100, 2)}% · p95 {formatNumber(item.p95 * 100, 2)}%</span>
          <span>uncertainty {formatNumber(item.uncertainty * 100, 2)}% · ambiguous {formatNumber((item.pAmbiguousBarrier ?? 0) * 100, 1)}%</span>
        </div>)}</div>
        <div className="external-meta">
          <span>reference {forecast.referenceMinute ?? forecast.asOf ?? 'n/a'}</span>
          <span>available {forecast.availableAt ?? 'n/a'}</span>
          {pointInTime && <span>{pointInTime}</span>}
        </div>
      </>}
    <footer>
      <span>{latency > 0 ? `${formatNumber(latency, 0)} ms` : 'latency unavailable'}</span>
      {priorSessions > 0 && <span>{priorSessions} prior sessions</span>}
      <span>EXPERIMENTAL / NOT USED BY PRICE_CORE_V2</span>
    </footer>
  </div>;
}

export function ExternalResearch({intelligence}: {intelligence: IntelligenceResponse | null | undefined}) {
  return <Panel title="EXTERNAL RESEARCH" action={<StatusBadge label="NOT USED BY PRICE_CORE_V2" tone="warn"/>}>
    <p className="external-disclaimer">Kronos and BANKNIFTY Time Machine outputs are research evidence only. They never change the production PRICE_CORE_V2 forecast.</p>
    <div className="external-research-grid">
      <ResearchModel title="KRONOS ZERO-SHOT" forecast={intelligence?.kronosExperimental}/>
      <ResearchModel title="BANKNIFTY TIME MACHINE" forecast={intelligence?.timeMachineExperimental}/>
    </div>
  </Panel>;
}
