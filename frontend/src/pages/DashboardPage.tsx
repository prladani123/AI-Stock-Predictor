import { useCallback, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { BigMoveCard } from '../components/dashboard/BigMoveCard';
import { ExternalResearch } from '../components/dashboard/ExternalResearch';
import { ImportantLevels } from '../components/dashboard/ImportantLevels';
import { IntelligenceDrawer } from '../components/dashboard/IntelligenceDrawer';
import { MarketEvidence } from '../components/dashboard/MarketEvidence';
import { SessionSummary } from '../components/dashboard/SessionSummary';
import { SimilarMarkets } from '../components/dashboard/SimilarMarkets';
import { NewsIntelligence } from '../components/dashboard/NewsIntelligence';
import { MarketChart } from '../components/charts/MarketChart';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, Panel, StatusBadge } from '../components/States';
import { formatIst, formatNumber, useApi } from '../hooks';
import type { IntelligenceHorizon } from '../types';

export function DashboardPage() {
  const dashboard = useApi(useCallback(() => api.dashboard(), []), 15000);
  const candles = useApi(useCallback(() => api.candles(500), []), 15000);
  const [selected, setSelected] = useState<IntelligenceHorizon | null>(null);
  const [showKronos, setShowKronos] = useState(false);
  if (dashboard.loading && !dashboard.data) return <LoadingState/>;
  if (dashboard.error && !dashboard.data) return <ErrorState message={dashboard.error} retry={dashboard.refresh}/>;
  const data = dashboard.data;
  const intelligence = data?.intelligence;
  const change = data?.priceChange;
  const kronosAvailable = Boolean(intelligence?.kronosExperimental.available && intelligence.kronosExperimental.forecastPath?.length);
  return <div className="page workstation">
    <PageTitle eyebrow="BANKNIFTY DECISION WORKSTATION" title="Market Intelligence" detail="Meaningful-move probability, direction and live derivatives context. Analysis only." actions={<button onClick={() => { void dashboard.refresh(); void candles.refresh(); }}><RefreshCw size={15}/>Refresh</button>}/>
    <section className="market-hero">
      <div><span>BANKNIFTY</span><strong>{formatNumber(data?.lastPrice)}</strong><small className={change != null && change < 0 ? 'negative' : 'positive'}>{change == null ? 'No completed candle' : `${change >= 0 ? '+' : ''}${formatNumber(change)} / ${formatNumber(data?.priceChangePercent)}%`}</small></div>
      <div className="hero-bias"><span>SESSION BIAS</span><strong className={intelligence?.sessionBias.direction.toLowerCase()}>{intelligence?.sessionBias.direction ?? 'NEUTRAL'} {intelligence ? `${Math.round(intelligence.sessionBias.confidence * 100)}%` : ''}</strong><small>{intelligence?.marketRegime.replaceAll('_', ' ') ?? 'UNKNOWN REGIME'}</small></div>
      <div className="hero-state"><StatusBadge label={data?.marketState ?? 'OFFLINE'} tone={data?.live.connected ? 'good' : 'neutral'}/><b>{data?.dataMode ?? 'OFFLINE'}</b><small>{formatIst(data?.latestCandle?.timestamp)}</small></div>
    </section>
    <Panel className="chart-panel" title="BANKNIFTY / 1 MINUTE" action={kronosAvailable ? <div className="segmented"><button className={!showKronos ? 'active' : ''} onClick={() => setShowKronos(false)}>Market</button><button className={showKronos ? 'active' : ''} onClick={() => setShowKronos(true)}>Experimental forecast</button></div> : undefined}>
      {showKronos && <div className="experimental-banner">EXPERIMENTAL KRONOS FORECAST — comparison only</div>}
      {candles.error ? <ErrorState message={candles.error} retry={candles.refresh}/> : <MarketChart candles={candles.data?.candles ?? []} intelligence={intelligence} showExperimental={showKronos}/>}
    </Panel>
    <section className="big-move-grid">{(intelligence?.horizons ?? []).map(signal => <BigMoveCard key={signal.minutes} signal={signal} regime={intelligence?.marketRegime ?? 'UNKNOWN'} onOpen={() => setSelected(signal)}/>)}</section>
    <ExternalResearch intelligence={intelligence}/>
    <SessionSummary intelligence={intelligence}/>
    <div><h2 className="section-label">MARKET EVIDENCE</h2><MarketEvidence intelligence={intelligence}/></div>
    <SimilarMarkets memory={intelligence?.memory?.numerical}/>
    <NewsIntelligence news={intelligence?.memory?.news} refresh={() => { void api.newsRefresh().then(() => dashboard.refresh()); }}/>
    <div><h2 className="section-label">IMPORTANT LEVELS</h2><ImportantLevels levels={intelligence?.levels}/></div>
    {selected && intelligence && <IntelligenceDrawer signal={selected} intelligence={intelligence} close={() => setSelected(null)}/>} 
  </div>;
}
