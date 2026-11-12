import { formatNumber } from '../../hooks';
import type { OptionRow } from '../../types';

export function OptionOIChart({rows}: {rows: OptionRow[]}) {
  const max = Math.max(1, ...rows.flatMap(row => [row.ce?.oi ?? 0, row.pe?.oi ?? 0]));
  return <div className="oi-chart" aria-label="CE and PE open interest by strike">
    <div className="oi-legend"><span className="ce">CE OI</span><span className="pe">PE OI</span></div>
    {rows.map(row => <div className={`oi-row ${row.atm ? 'atm' : ''}`} key={row.strike}>
      <div className="oi-side ce"><span style={{width: `${((row.ce?.oi ?? 0) / max) * 100}%`}}/><b>{formatNumber(row.ce?.oi, 0)}</b></div>
      <em>{formatNumber(row.strike, 0)}</em>
      <div className="oi-side pe"><span style={{width: `${((row.pe?.oi ?? 0) / max) * 100}%`}}/><b>{formatNumber(row.pe?.oi, 0)}</b></div>
    </div>)}
  </div>;
}
