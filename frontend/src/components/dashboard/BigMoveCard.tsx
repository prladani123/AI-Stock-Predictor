import { ArrowDown, ArrowUp, Clock3 } from 'lucide-react';
import { formatIst, formatNumber } from '../../hooks';
import type { IntelligenceHorizon } from '../../types';

const pct = (value: number | null) => value == null ? 'Unavailable' : `${Math.round(value * 100)}%`;

export function BigMoveCard({signal, regime, onOpen}: {signal: IntelligenceHorizon; regime: string; onOpen: () => void}) {
  const DirectionIcon = signal.direction === 'UP' ? ArrowUp : ArrowDown;
  return <button className={`big-move-card ${signal.direction?.toLowerCase() ?? 'wait'} ${signal.actionState.toLowerCase()}`} onClick={onOpen}>
    <header><span>NEXT {signal.minutes} MIN</span><b className={`action-state ${signal.actionState.toLowerCase()}`}>{signal.actionState.replaceAll('_', ' ')}</b></header>
    <div className="primary-stat"><small>BIG MOVE</small><strong>{pct(signal.bigMoveProbability)}</strong><span>quiet {pct(signal.quietProbability)}</span></div>
    <div><small>DIRECTION</small><strong className="direction">{signal.direction ? <><DirectionIcon size={18}/>{signal.direction} {pct(signal.directionProbability)}</> : 'WAIT'}</strong></div>
    <div><small>EXPECTED MOVE</small><strong>{signal.expectedMovePoints == null ? '-' : `~${formatNumber(signal.expectedMovePoints, 0)} pts`}</strong><span>{signal.expectedMovePercent == null ? '-' : `${formatNumber(signal.expectedMovePercent * 100, 2)}%`}</span></div>
    <footer><span>{regime.replaceAll('_', ' ')}</span><time><Clock3 size={12}/>{formatIst(signal.asOf)}</time></footer>
  </button>;
}
