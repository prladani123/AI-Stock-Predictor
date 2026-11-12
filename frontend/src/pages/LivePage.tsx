import { useCallback, useState } from 'react';
import { Play, RefreshCw, Square } from 'lucide-react';
import { api } from '../api';
import { PageTitle } from '../components/PageTitle';
import { ErrorState, LoadingState, MetricCard, Panel } from '../components/States';
import { formatIst, useApi } from '../hooks';

export function LivePage() {
  const state = useApi(useCallback(() => api.live(), []), 5000);
  const options = useApi(useCallback(() => api.options(), []), 15000);
  const futures = useApi(useCallback(() => api.futures(), []), 15000);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const simulation = state.data?.collectorMode === 'SIMULATED' && state.data.connected;
  const realLive = state.data?.collectorMode === 'LIVE' && state.data.connected;
  const run = async (action: 'start' | 'stop') => {
    if (pending || realLive || (action === 'stop' && !simulation) || (action === 'start' && simulation)) return;
    setActionError(null); setPending(true);
    try { state.setData(action === 'start' ? await api.startSimulation() : await api.stopSimulation()); }
    catch (value) { setActionError(value instanceof Error ? value.message : 'Runtime action failed.'); }
    finally { setPending(false); }
  };
  if (state.loading && !state.data) return <LoadingState/>;
  if (state.error && !state.data) return <ErrorState message={state.error} retry={state.refresh}/>;
  const live = state.data;
  return <div className="page"><PageTitle eyebrow="MARKET CONNECTION" title="Live Market" detail="Current feed state, freshness and data gaps." actions={<><button onClick={() => { void state.refresh(); void options.refresh(); void futures.refresh(); }}><RefreshCw size={15}/>Refresh</button><button className="primary" disabled={pending || realLive || simulation || !live} onClick={() => void run('start')}><Play size={15}/>Start simulation</button><button disabled={pending || realLive || !simulation} onClick={() => void run('stop')}><Square size={15}/>Stop simulation</button></>}/>
    {actionError && <div className="error-notice">{actionError}</div>}{live?.recentQualityWarning && <div className="notice">{live.recentQualityWarning}</div>}
    <div className="metrics live-metrics"><MetricCard label="MODE" value={simulation ? 'SIMULATION' : realLive ? 'REAL LIVE' : 'OFFLINE'} detail={live?.state ?? 'Unavailable'}/><MetricCard label="LATEST TICK" value={formatIst(live?.lastTick)}/><MetricCard label="LATEST CANDLE" value={formatIst(live?.lastCompletedCandle)}/><MetricCard label="OPTIONS" value={options.data?.freshnessStatus ?? 'UNAVAILABLE'} detail={formatIst(options.data?.asOf)}/><MetricCard label="FUTURES" value={futures.data?.available ? 'AVAILABLE' : 'UNAVAILABLE'} detail={formatIst(futures.data?.asOf)}/><MetricCard label="ACTIVE GAPS" value={live?.activeGapCount ?? 0} detail={`${live?.gapCount ?? 0} total detected`}/></div>
    <details className="technical"><summary>Technical details</summary><Panel><dl className="detail-grid wide"><dt>Started</dt><dd>{formatIst(live?.startedAt)}</dd><dt>Stopped</dt><dd>{formatIst(live?.stoppedAt)}</dd><dt>Ticks</dt><dd>{live?.receivedTickCount ?? 0}</dd><dt>Reconnects</dt><dd>{live?.reconnectCount ?? 0}</dd><dt>Desired tokens</dt><dd>{live?.desiredSubscriptions?.join(', ') || '-'}</dd><dt>Active tokens</dt><dd>{live?.activeSubscriptions?.join(', ') || '-'}</dd></dl></Panel></details>
  </div>;
}
