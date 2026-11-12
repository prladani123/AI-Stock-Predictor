import type { EventIntelligence, NewsEvent, EventImpactDetail } from '../types';
import { Link } from 'react-router-dom';

const time = (value?:string|null) => value ? new Date(value).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})+' IST' : 'UNKNOWN';
const percent = (value:unknown) => typeof value === 'number' ? `${(value*100).toFixed(3)}%` : '—';
const text = (value:unknown) => typeof value === 'string' || typeof value === 'number' ? String(value).replaceAll('_',' ') : '—';
const object = (value:unknown):Record<string,unknown> => value && typeof value === 'object' ? value as Record<string,unknown> : {};

export function EventEvidence({event, memory, impact}:{event:NewsEvent;memory:EventIntelligence|null;impact?:EventImpactDetail|null}) {
  const reaction=event.reaction, returns=object(reaction.returns), moves=object(reaction.big_move);
  const history=memory?.historicalMemory;
  const sources=event.source_metadata?.sources ?? (event.source_url ? [{name:event.source_name ?? 'Source',url:event.source_url}] : []);
  const state=String(reaction.status ?? 'UNMEASURABLE');
  const pre=impact?.study?.pre_event_state;
  const features=impact?.study?.event_features;
  return <div className="memory-detail">
    <section><h3>PRE-EVENT MARKET</h3>{pre ? <>
      <p>Frozen at {time(impact?.study?.event_known_at)}. Only receipt-eligible completed evidence.</p>
      <div className="news-stats">{[['Price','price'],['Breadth','constituents'],['Futures','futures'],['Options','options'],['VIX / global','globals']].map(([name,key]) =>
        <details key={key}><summary>{name}</summary>{pre[key] == null ? <p>Unavailable at event time</p> : <pre style={{whiteSpace:'pre-wrap'}}>{JSON.stringify(pre[key],null,2)}</pre>}</details>)}</div>
      <p>Regime: {text(pre.regime)} · Market structure: {text(object(pre.marketStructure).regime)} / {text(object(pre.marketStructure).phase)}</p>
    </> : <p>{impact?.status ?? 'Pre-event capture unavailable'}</p>}</section>
    {features && <section><h3>EVENT CONTEXT</h3><p>{text(features.primaryCategory)} · {text(features.stance)} · {text(features.scope)}</p><p>Novelty: {text(features.noveltyClass)} · Source authority: {text(features.sourceAuthority)}</p><p>Affected constituents: {Array.isArray(features.affectedSymbols) ? features.affectedSymbols.join(', ') || 'None identified' : 'Unknown'}</p></section>}
    <section><h3>HISTORICAL REACTION PRIOR</h3><p>Descriptive outcomes known before this event; not prediction probabilities.</p>
      <div style={{overflowX:'auto'}}><table><thead><tr><th>Horizon</th><th>Samples</th><th>Mean / median</th><th>Positive / negative</th><th>Big move</th><th>Status</th></tr></thead><tbody>
        {[5,10,20,30].map(h => {const row=impact?.historicalReactionPrior?.horizons[String(h)];return <tr key={h}><td>{h}m</td><td>{row?.sampleCount ?? 0}</td><td>{percent(row?.meanReturn)} / {percent(row?.medianReturn)}</td><td>{percent(row?.positiveRate)} / {percent(row?.negativeRate)}</td><td>{h===5 ? 'N/A' : percent(row?.bigMoveRate)}</td><td>{text(row?.status ?? 'INSUFFICIENT_HISTORY')}</td></tr>;})}
      </tbody></table></div>
    </section>
    <section><h3>EVENT</h3><h2>{event.headline_or_summary}</h2><p>{text(event.event_category)} · {event.secondary_categories?.map(text).join(' · ')}</p><p>Stance: {text(event.stance)} · Scope: {text(event.scope)} · Relevance: {event.relevance}</p></section>
    <section><h3>TIMING</h3><dl><dt>Published</dt><dd>{time(event.published_at)}</dd><dt>First seen</dt><dd>{time(event.first_seen_at)}</dd><dt>Known at</dt><dd>{time(event.known_at)}</dd><dt>Knowledge phase</dt><dd>{text(event.knowledge_phase)}</dd><dt>Timing confidence</dt><dd>{text(event.timing_confidence ?? event.timing_precision)}</dd></dl><p>{event.predictive_eligible ? 'PREDICTIVE ELIGIBLE' : 'CONTEXT ONLY'}</p></section>
    <section><h3>ACTUAL REACTION · {state.startsWith('PARTIAL') ? 'PARTIAL' : state.startsWith('COMPLETE') ? 'COMPLETE' : state === 'PENDING' ? 'PENDING' : 'UNMEASURABLE'}</h3>
      <div className="news-stats">{[5,10,20,30].map(h => <span key={h}>+{h}m <b>{percent(returns[String(h)])}</b>{h!==5 && <small>Big Move: {text(object(moves[String(h)]).label)}</small>}</span>)}</div>
      <p>Max up: {percent(reaction.max_up_excursion)} · Max down: {percent(reaction.max_down_excursion)} · Volatility expansion: {typeof reaction.volatility_expansion === 'number' ? reaction.volatility_expansion.toFixed(2)+'×' : '—'}</p>
      {reaction.reason != null && <p>{text(reaction.reason)}</p>}
    </section>
    <section><h3>HISTORICAL EVENT MEMORY</h3><p>{history?.matchedEvents ?? 0} matched events · Similarity: {history?.eventSimilarityStrength ?? 'UNAVAILABLE'}</p>
      <div style={{overflowX:'auto'}}><table><thead><tr><th>Horizon</th><th>Samples</th><th>Mean return</th><th>Big Move rate</th><th>Up / Down</th></tr></thead><tbody>{[5,10,20,30].map(h => {const row=history?.historicalReaction[String(h)];return <tr key={h}><td>{h}m</td><td>{row?.sampleSize ?? 0}</td><td>{percent(row?.averageReturn)}</td><td>{percent(row?.bigMoveRate)}</td><td>{percent(row?.upRate)} / {percent(row?.downRate)}</td></tr>;})}</tbody></table></div>
      {history?.reason && <p>{text(history.reason)}</p>}
      {history?.topMatches.map(item => <p key={item.eventId}><Link to={`/news?event=${encodeURIComponent(item.eventId)}`}>{item.date} · {item.summary}</Link> · {percent(item.similarity)} similarity</p>)}
    </section>
    <section><h3>MARKET CONFIRMATION</h3><p>{text(memory?.marketConfirmation?.state ?? 'INSUFFICIENT_DATA')}</p><p className="muted">Current market evidence, not a historical causal claim.</p>{memory?.marketConfirmation?.drivers.map((driver,i)=><p key={i}>{driver}</p>)}</section>
    <section><h3>SOURCES</h3>{sources.filter(s=>/^https?:\/\//.test(s.url)).map((s,i)=><p key={i}><a href={s.url} target="_blank" rel="noreferrer">{s.name}</a></p>)}</section>
  </div>;
}
