import { useEffect, useRef } from 'react';
import { CandlestickSeries, ColorType, HistogramSeries, LineSeries, LineStyle, createChart, createSeriesMarkers, type UTCTimestamp, type SeriesMarker } from 'lightweight-charts';
import type { Candle, IntelligenceResponse } from '../../types';

const utc = (value: string) => Math.floor(new Date(value).getTime() / 1000) as UTCTimestamp;

export function MarketChart({candles, intelligence, showExperimental = false}: {candles: Candle[]; intelligence?: IntelligenceResponse | null; showExperimental?: boolean}) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!host.current || !candles.length) return;
    const chart = createChart(host.current, {autoSize: true,
      layout: {background: {type: ColorType.Solid, color: '#08131f'}, textColor: '#8499ad'},
      grid: {vertLines: {color: '#13263a'}, horzLines: {color: '#13263a'}},
      rightPriceScale: {borderColor: '#24405a'},
      crosshair: {vertLine: {color: '#54749b'}, horzLine: {color: '#54749b'}},
      timeScale: {timeVisible: true, secondsVisible: false, borderColor: '#24405a'}});
    const series = chart.addSeries(CandlestickSeries, {upColor: '#29c889', downColor: '#f25e6b', borderVisible: false, wickUpColor: '#29c889', wickDownColor: '#f25e6b'});
    series.setData(candles.map(c => ({time: utc(c.timestamp), open: c.open, high: c.high, low: c.low, close: c.close})));
    const volume = chart.addSeries(HistogramSeries, {priceFormat: {type: 'volume'}, priceScaleId: '', color: '#355b89'});
    volume.priceScale().applyOptions({scaleMargins: {top: .84, bottom: 0}});
    volume.setData(candles.filter(c => c.volume != null).map(c => ({time: utc(c.timestamp), value: c.volume ?? 0, color: c.close >= c.open ? '#1d7257aa' : '#7d3542aa'})));

    const latest = candles[candles.length - 1];
    const latestTime = utc(latest.timestamp);
    series.createPriceLine({price: latest.close, color: '#e6f0fa', lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: 'CURRENT'});
    const levels = intelligence?.levels;
    const priceLines: Array<[string, number | null | undefined, string, LineStyle]> = [
      ['VWAP', levels?.vwap, '#56cbd6', LineStyle.Dotted], ['RESISTANCE', levels?.resistance, '#f05d68', LineStyle.Dashed],
      ['SUPPORT', levels?.support, '#26c281', LineStyle.Dashed], ['CE OI WALL', levels?.strongCeOi, '#f09b55', LineStyle.Dashed],
      ['PE OI WALL', levels?.strongPeOi, '#54d4a0', LineStyle.Dashed], ['ATM', levels?.atm, '#a68cef', LineStyle.Dotted],
    ];
    priceLines.forEach(([title, price, color, style]) => { if (price != null) series.createPriceLine({price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title}); });

    const markers: SeriesMarker<UTCTimestamp>[] = [];
    intelligence?.horizons.filter(item => item.available && item.direction).forEach(item => markers.push({time: item.asOf ? utc(item.asOf) : latestTime, position: item.direction === 'UP' ? 'belowBar' as const : 'aboveBar' as const, color: item.direction === 'UP' ? '#38df9b' : '#ff7280', shape: item.direction === 'UP' ? 'arrowUp' as const : 'arrowDown' as const, text: `${item.minutes}m ${item.direction}`}));
    const newsEvent = intelligence?.memory?.news.latestHighImpactEvent;
    const eventTime = newsEvent?.firstSeenAt ? utc(newsEvent.firstSeenAt) : null;
    if (newsEvent?.relevance === 'HIGH' && eventTime != null && eventTime >= utc(candles[0].timestamp) && eventTime <= latestTime) {
      markers.push({time: eventTime, position: 'aboveBar' as const, color: '#efb84f', shape: 'circle' as const, text: `${newsEvent.category.replaceAll('_',' ')} NEWS`});
      chart.subscribeClick(param => { if (param.time === eventTime) window.location.assign(`/news?event=${encodeURIComponent(newsEvent.eventId)}`); });
    }
    if (markers.length) createSeriesMarkers(series, markers.sort((left,right)=>Number(left.time)-Number(right.time)));

    intelligence?.horizons.filter(item => item.available && item.expectedMovePoints != null).forEach((item, index) => {
      const start = item.asOf ? utc(item.asOf) : latestTime;
      const end = (Number(start) + item.minutes * 60) as UTCTimestamp;
      const base = item.referencePrice ?? latest.close;
      const color = ['#55d6e2', '#efb84f', '#b785ff'][index] ?? '#55d6e2';
      const upper = chart.addSeries(LineSeries, {color, lineWidth: 1, lineStyle: LineStyle.Dashed, title: `${item.minutes}m upper band`, lastValueVisible: false});
      const lower = chart.addSeries(LineSeries, {color, lineWidth: 1, lineStyle: LineStyle.Dashed, title: `${item.minutes}m lower band`, lastValueVisible: false});
      upper.setData([{time: start, value: base + item.expectedMovePoints!}, {time: end, value: base + item.expectedMovePoints!}]);
      lower.setData([{time: start, value: base - item.expectedMovePoints!}, {time: end, value: base - item.expectedMovePoints!}]);
    });

    const experimental = intelligence?.kronosExperimental;
    if (showExperimental && experimental?.available && experimental.forecastPath?.length) {
      const forecast = chart.addSeries(LineSeries, {color: '#b785ff', lineWidth: 2, lineStyle: LineStyle.Dotted, title: 'EXPERIMENTAL KRONOS FORECAST'});
      forecast.setData(experimental.forecastPath.map(item => ({time: utc(item.timestamp), value: item.close})));
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [candles, intelligence, showExperimental]);
  return candles.length ? <div className="chart" ref={host}/> : <div className="chart-empty">No BANKNIFTY candles available.</div>;
}
