import { CandlestickChart, ChartNoAxesCombined, Gauge, Layers3 } from 'lucide-react';
import { formatNumber } from '../../hooks';
import type { IntelligenceResponse } from '../../types';

const signed = (value: number | null | undefined, percent = false) => value == null ? '-' : `${value > 0 ? '+' : ''}${formatNumber(value, 2)}${percent ? '%' : ''}`;

export function MarketEvidence({intelligence}: {intelligence: IntelligenceResponse | null | undefined}) {
  const e = intelligence?.evidence;
  const blocks = [
    {title: 'PRICE STRUCTURE', icon: CandlestickChart, lead: e?.price.shortTrend ?? 'Unavailable', rows: [['VWAP', e?.price.vwapRelationship ?? '-'], ['5m return', signed(e?.price.return5m == null ? null : e.price.return5m * 100, true)], ['Range expansion', formatNumber(e?.price.rangeExpansion)]]},
    {title: 'FUTURES', icon: ChartNoAxesCombined, lead: e?.futures.buildup.replaceAll('_', ' ') ?? 'Unavailable', rows: [['Price', formatNumber(e?.futures.price)], ['Premium', `${signed(e?.futures.premium)} pts`], ['OI change 1m', signed(e?.futures.oiChange1m)], ['Volume', formatNumber(e?.futures.volume, 0)]]},
    {title: 'OPTIONS', icon: Layers3, lead: e?.options.pcrOi == null ? 'Unavailable' : `PCR ${formatNumber(e.options.pcrOi)}`, rows: [['CE / PE OI', `${formatNumber(e?.options.totalCeOi, 0)} / ${formatNumber(e?.options.totalPeOi, 0)}`], ['CE / PE delta OI', `${signed(e?.options.totalCeOiChange)} / ${signed(e?.options.totalPeOiChange)}`], ['ATM straddle', formatNumber(e?.options.atmStraddle)], ['CE / PE wall', `${formatNumber(e?.options.strongCeOi, 0)} / ${formatNumber(e?.options.strongPeOi, 0)}`]]},
    {title: 'REGIME', icon: Gauge, lead: intelligence?.marketRegime.replaceAll('_', ' ') ?? 'UNKNOWN', rows: [['Session', intelligence?.sessionBias.direction ?? '-'], ['RV 5m', e?.volatility.realized5m == null ? '-' : `${formatNumber(e.volatility.realized5m * 100, 3)}%`], ['RV 20m', e?.volatility.realized20m == null ? '-' : `${formatNumber(e.volatility.realized20m * 100, 3)}%`], ['VIX', e?.volatility.vixStatus.replaceAll('_', ' ') ?? 'Pending']]},
  ];
  return <section className="evidence-grid">{blocks.map(({title, icon: Icon, lead, rows}) => <article className="evidence-card" key={title}><header><Icon size={16}/><span>{title}</span></header><strong>{lead}</strong><dl>{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></article>)}</section>;
}
