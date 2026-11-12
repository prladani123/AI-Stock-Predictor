import { Panel } from '../States';
import type { MemorySummary } from '../../types';

const percent = (value: number | null | undefined) => value == null ? '—' : `${Math.round(value * 100)}%`;

export function SimilarMarkets({memory}: {memory?: MemorySummary}) {
  if (!memory?.available) return <Panel title="SIMILAR HISTORICAL MARKETS"><p className="muted">Historical comparison unavailable for the current feature state.</p></Panel>;
  const outcomes = memory.historicalOutcome ?? {};
  return <Panel title="SIMILAR HISTORICAL MARKETS" action={<span className="muted">{memory.similarityStrength} similarity</span>}>
    <div className="memory-summary"><strong>{memory.matchedEpisodes} similar setups found</strong><small>Descriptive evidence only; this does not change V2 probabilities.</small></div>
    <div className="memory-rates"><span>10m BIG MOVE <b>{percent(outcomes.bigMove10Rate)}</b></span><span>20m BIG MOVE <b>{percent(outcomes.bigMove20Rate)}</b></span><span>30m BIG MOVE <b>{percent(outcomes.bigMove30Rate)}</b></span><span>{memory.directionBias ?? 'DIRECTION'} <b>{percent(memory.directionRate)}</b></span></div>
    <div className="memory-matches">{memory.topMatches.slice(0, 3).map(match => <a href={`/memory?date=${match.date}`} key={match.asOf}><span>{match.date} · {match.regime.replaceAll('_', ' ')}</span><b>{Math.round(match.similarity * 100)}%</b></a>)}</div>
  </Panel>;
}
