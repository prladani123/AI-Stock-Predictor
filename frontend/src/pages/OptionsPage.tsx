import { useCallback, useMemo, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../api';
import { OptionOIChart } from '../components/charts/OptionOIChart';
import { PageTitle } from '../components/PageTitle';
import { EmptyState, ErrorState, LoadingState, MetricCard, Panel } from '../components/States';
import { formatIst, formatNumber, useApi } from '../hooks';

const deltaClass = (value: number | null | undefined) => value == null || value === 0 ? '' : value > 0 ? 'positive' : 'negative';

export function OptionsPage() {
  const [selectedExpiry, setSelectedExpiry] = useState(''); const [range, setRange] = useState(5);
  const state = useApi(useCallback(() => api.options(selectedExpiry), [selectedExpiry]), 15000);
  const visible = useMemo(() => { const rows = state.data?.rows ?? []; const atm = rows.findIndex(row => row.atm); return atm < 0 ? rows : rows.slice(Math.max(0, atm - range), atm + range + 1); }, [state.data, range]);
  if (state.loading && !state.data) return <LoadingState/>;
  if (state.error && !state.data) return <ErrorState message={state.error} retry={state.refresh}/>;
  const options = state.data;
  return <div className="page"><PageTitle eyebrow="DERIVATIVES / OI" title="BANKNIFTY Option Chain" detail="Paired calls and puts from the selected stored ATM universe." actions={<button onClick={() => void state.refresh()}><RefreshCw size={15}/>Refresh</button>}/>
    {!options?.available && <div className="notice">{options?.reason ?? 'No eligible option snapshot is stored.'}</div>}
    <div className="option-summary"><MetricCard label="BANKNIFTY" value={formatNumber(options?.underlying)}/><MetricCard label="ATM / EXPIRY" value={formatNumber(options?.atmStrike, 0)} detail={options?.expiry ?? '-'}/><MetricCard label="PCR OI" value={formatNumber(options?.pcrOi)} detail={`CE ${formatNumber(options?.totalCeOi, 0)} / PE ${formatNumber(options?.totalPeOi, 0)}`}/><MetricCard label="ATM STRADDLE" value={formatNumber(options?.atmStraddle)} detail={formatIst(options?.asOf)}/><MetricCard label="FRESHNESS" value={options?.freshnessStatus ?? 'UNKNOWN'}/></div>
    <div className="options-layout"><Panel title="OI CONCENTRATION"><OptionOIChart rows={visible}/></Panel><Panel title="IMPORTANT OI LEVELS"><dl className="level-pairs"><div><dt>Strong CE OI</dt><dd className="negative">{formatNumber(options?.strongCeOi, 0)}</dd></div><div><dt>Strong PE OI</dt><dd className="positive">{formatNumber(options?.strongPeOi, 0)}</dd></div><div><dt>Total CE delta OI</dt><dd className={deltaClass(options?.totalCeOiChange)}>{formatNumber(options?.totalCeOiChange, 0)}</dd></div><div><dt>Total PE delta OI</dt><dd className={deltaClass(options?.totalPeOiChange)}>{formatNumber(options?.totalPeOiChange, 0)}</dd></div></dl></Panel></div>
    <Panel title="CALL / STRIKE / PUT" action={<div className="actions">{(options?.eligibleExpiries?.length ?? 0) > 1 && <select aria-label="Option expiry" value={selectedExpiry || options?.expiry || ''} onChange={event => setSelectedExpiry(event.target.value)}>{options?.eligibleExpiries?.map(expiry => <option key={expiry}>{expiry}</option>)}</select>}<div className="segmented">{[1,3,5].map(value => <button key={value} className={range === value ? 'active' : ''} onClick={() => setRange(value)}>ATM +/-{value}</button>)}</div></div>}>
      {!visible.length ? <EmptyState title="No option rows" detail="Start the simulation or connect a valid stored snapshot."/> : <div className="table-scroll"><table className="option-table trader-chain"><thead><tr><th>DELTA OI</th><th>OI</th><th>VOL</th><th>LTP</th><th>BID</th><th>ASK</th><th>CALL</th><th>STRIKE</th><th>PUT</th><th>BID</th><th>ASK</th><th>LTP</th><th>VOL</th><th>OI</th><th>DELTA OI</th></tr></thead><tbody>{visible.map(row => { const stale = [row.ce?.qualityStatus, row.pe?.qualityStatus].some(status => status && status !== 'PASS'); const ceWall = row.strike === options?.strongCeOi; const peWall = row.strike === options?.strongPeOi; return <tr key={row.strike} className={`${row.atm ? 'atm ' : ''}${stale ? 'stale' : ''}`}><td className={deltaClass(row.ce?.oiChange)}>{formatNumber(row.ce?.oiChange,0)}</td><td className={ceWall ? 'oi-wall ce-wall' : ''}>{formatNumber(row.ce?.oi,0)}</td><td>{formatNumber(row.ce?.volume,0)}</td><td>{formatNumber(row.ce?.ltp)}</td><td>{formatNumber(row.ce?.bid)}</td><td>{formatNumber(row.ce?.ask)}</td><th>CE</th><th className="strike">{formatNumber(row.strike,0)}{row.atm && <em>ATM</em>}</th><th>PE</th><td>{formatNumber(row.pe?.bid)}</td><td>{formatNumber(row.pe?.ask)}</td><td>{formatNumber(row.pe?.ltp)}</td><td>{formatNumber(row.pe?.volume,0)}</td><td className={peWall ? 'oi-wall pe-wall' : ''}>{formatNumber(row.pe?.oi,0)}</td><td className={deltaClass(row.pe?.oiChange)}>{formatNumber(row.pe?.oiChange,0)}</td></tr>;})}</tbody></table></div>}
    </Panel>
  </div>;
}
