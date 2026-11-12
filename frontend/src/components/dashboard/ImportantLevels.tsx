import { formatNumber } from '../../hooks';
import type { ImportantLevels as Levels } from '../../types';

export function ImportantLevels({levels}: {levels?: Levels}) {
  const items: Array<[string, number | null | undefined, string]> = [
    ['BANKNIFTY', levels?.current, 'current'], ['VWAP', levels?.vwap, 'vwap'], ['ATM', levels?.atm, 'atm'],
    ['RESISTANCE', levels?.resistance, 'resistance'], ['SUPPORT', levels?.support, 'support'],
    ['STRONG CE OI', levels?.strongCeOi, 'resistance'], ['STRONG PE OI', levels?.strongPeOi, 'support'],
    ['OPENING HIGH', levels?.openingHigh, 'high'], ['OPENING LOW', levels?.openingLow, 'low'],
    ['SESSION HIGH', levels?.sessionHigh, 'high'], ['SESSION LOW', levels?.sessionLow, 'low'],
  ];
  return <section className="levels-strip">{items.map(([label, value, tone]) => <div key={label} className={tone}><span>{label}</span><b>{formatNumber(value, 0)}</b></div>)}</section>;
}
