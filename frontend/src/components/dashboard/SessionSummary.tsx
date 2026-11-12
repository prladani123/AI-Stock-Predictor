import { Activity, Gauge, TrendingDown, TrendingUp } from 'lucide-react';
import { formatNumber } from '../../hooks';
import type { IntelligenceResponse } from '../../types';

export function SessionSummary({intelligence}: {intelligence: IntelligenceResponse | null | undefined}) {
  const bias = intelligence?.sessionBias;
  const futures = intelligence?.evidence?.futures;
  const options = intelligence?.evidence?.options;
  const nextLevel = bias?.direction === 'BEARISH' ? intelligence?.levels?.support : bias?.direction === 'BULLISH' ? intelligence?.levels?.resistance : null;
  const BiasIcon = bias?.direction === 'BULLISH' ? TrendingUp : bias?.direction === 'BEARISH' ? TrendingDown : Gauge;
  return <section className="session-summary">
    <div className="session-title"><span className="eyebrow">TODAY / CURRENT SESSION</span><strong className={bias?.direction.toLowerCase() ?? 'neutral'}><BiasIcon size={22}/>{bias?.direction ?? 'NEUTRAL'} {bias ? `${Math.round(bias.confidence * 100)}%` : ''}</strong></div>
    <div><span>REGIME</span><b>{intelligence?.marketRegime.replaceAll('_', ' ') ?? 'UNKNOWN'}</b></div>
    <div><span>BANKNIFTY</span><b>{intelligence?.banknifty?.vwapRelationship === 'UNAVAILABLE' ? 'VWAP unavailable' : `${intelligence?.banknifty?.vwapRelationship ?? '-'} VWAP`}</b></div>
    <div><span>FUTURES</span><b>{futures?.buildup.replaceAll('_', ' ') ?? 'Unavailable'}</b></div>
    <div><span>OPTIONS</span><b>{options?.pcrOi == null ? 'Unavailable' : `PCR ${formatNumber(options.pcrOi)}`}</b></div>
    <div><span>NEXT LEVEL</span><b>{formatNumber(nextLevel, 0)}</b></div>
    <Activity className="session-watermark"/>
  </section>;
}
