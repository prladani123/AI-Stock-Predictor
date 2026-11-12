import { X } from 'lucide-react';
import { formatIst, formatNumber } from '../../hooks';
import type { IntelligenceHorizon, IntelligenceResponse } from '../../types';

const pct = (value: number | null, scale = 100) => value == null ? '-' : `${formatNumber(value * scale, 2)}%`;
const show = (value: number | null | undefined, digits = 2) => formatNumber(value, digits);

export function IntelligenceDrawer({signal, intelligence, close}: {signal: IntelligenceHorizon; intelligence: IntelligenceResponse; close: () => void}) {
  const evidence = intelligence.evidence;
  return <div className="drawer-backdrop" role="presentation" onMouseDown={event => event.target === event.currentTarget && close()}>
    <aside className="intelligence-drawer" role="dialog" aria-modal="true" aria-label={`${signal.minutes} minute intelligence`}>
      <button className="close" aria-label="Close" onClick={close}><X size={17}/></button>
      <span className="eyebrow">BIG MOVE V2 / {signal.minutes} MINUTES</span>
      <h2>{signal.direction ?? signal.state.replaceAll('_', ' ')}</h2>
      <div className="drawer-summary"><div><span>Big move</span><b>{pct(signal.bigMoveProbability)}</b></div><div><span>Direction</span><b>{signal.direction ? `${signal.direction} ${pct(signal.directionProbability)}` : '-'}</b></div><div><span>Expected</span><b>{signal.expectedMovePoints == null ? '-' : `~${show(signal.expectedMovePoints, 0)} pts`}</b></div><div><span>Action</span><b>{signal.actionState.replaceAll('_', ' ')}</b></div></div>
      <section><h3>Market context</h3><dl className="detail-grid"><dt>Regime</dt><dd>{intelligence.marketRegime.replaceAll('_', ' ')}</dd><dt>Session bias</dt><dd>{intelligence.sessionBias.direction} / {pct(intelligence.sessionBias.confidence)}</dd><dt>Prediction time</dt><dd>{formatIst(signal.asOf)}</dd><dt>Expected percent</dt><dd>{pct(signal.expectedMovePercent)}</dd></dl></section>
      <section><h3>Supporting evidence</h3>{!evidence ? <p className="muted">Point-in-time evidence is unavailable.</p> : <dl className="detail-grid"><dt>Price / VWAP</dt><dd>{evidence.price.shortTrend} / {evidence.price.vwapRelationship}</dd><dt>5m momentum</dt><dd>{pct(evidence.price.return5m)}</dd><dt>Futures buildup</dt><dd>{evidence.futures.buildup.replaceAll('_', ' ')}</dd><dt>Futures premium</dt><dd>{show(evidence.futures.premium)} pts</dd><dt>Options PCR</dt><dd>{show(evidence.options.pcrOi)}</dd><dt>OI imbalance</dt><dd>{pct(evidence.options.oiChangeImbalance)}</dd><dt>CE / PE walls</dt><dd>{show(evidence.options.strongCeOi, 0)} / {show(evidence.options.strongPeOi, 0)}</dd><dt>Volatility 5m</dt><dd>{pct(evidence.volatility.realized5m)}</dd></dl>}</section>
      <section><h3>Why?</h3>{intelligence.why?.length ? <ul className="evidence-list">{intelligence.why.map(item => <li key={item}>{item}</li>)}</ul> : <p className="muted">No supported evidence statement is available.</p>}</section>
      <section><h3>Model</h3><dl className="detail-grid"><dt>Move detector</dt><dd>{signal.moveAlgorithm ?? 'Unavailable'}</dd><dt>Direction model</dt><dd>{signal.directionAlgorithm ?? 'Unavailable'}</dd><dt>Version</dt><dd>{signal.modelVersion ?? 'Unavailable'}</dd><dt>Quality</dt><dd>{signal.qualityStatus}</dd></dl></section>
    </aside>
  </div>;
}
