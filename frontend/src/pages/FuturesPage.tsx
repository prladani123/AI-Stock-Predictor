import { useCallback } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { EmptyState, ErrorState, LoadingState, MetricCard, Panel } from '../components/States';
import { formatIst, formatNumber, useApi } from '../hooks';

export function FuturesPage() {
  const state = useApi(useCallback(() => api.futures(), []), 15000); const data = state.data;
  return <div className="page"><PageTitle eyebrow="DERIVATIVES CONTEXT" title="BANKNIFTY Futures" detail="Latest completed futures minute and price/OI buildup state." actions={<button onClick={() => void state.refresh()}><RefreshCw size={15}/>Refresh</button>}/>
    {state.loading ? <LoadingState/> : state.error ? <ErrorState message={state.error} retry={state.refresh}/> : !data?.available ? <EmptyState title="Futures unavailable" detail={data?.reason ?? 'No completed futures snapshot exists.'}/> : <>
      <section className={`buildup-hero ${(data.buildup ?? 'neutral').toLowerCase()}`}><span>PRICE + OPEN INTEREST</span><strong>{data.buildup?.replaceAll('_', ' ') ?? 'NEUTRAL'}</strong><small>Deterministic market context, not trading advice</small></section>
      <div className="metrics futures-metrics"><MetricCard label="BANKNIFTY SPOT" value={formatNumber(data.indexPrice)}/><MetricCard label="FUTURES PRICE" value={formatNumber(data.ltp)} detail={data.contract}/><MetricCard label="PREMIUM" value={formatNumber(data.premium)} detail={`${formatNumber(data.premiumPercent)}%`}/><MetricCard label="OPEN INTEREST" value={formatNumber(data.oi,0)} detail={`delta ${formatNumber(data.oiChange,0)}`}/><MetricCard label="VOLUME" value={formatNumber(data.volume,0)} detail={formatIst(data.asOf)}/></div>
      <Panel title="CONTRACT CONTEXT"><dl className="detail-grid"><dt>Contract</dt><dd>{data.contract}</dd><dt>Expiry</dt><dd>{data.expiry}</dd><dt>Rollover selection</dt><dd>{data.rollover ? 'Yes' : 'No'}</dd><dt>Last completed minute</dt><dd>{formatIst(data.asOf)}</dd></dl></Panel>
    </>}
  </div>;
}
