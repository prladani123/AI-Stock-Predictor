import { Activity, BarChart3, Bot, BrainCircuit, CandlestickChart, ChartNoAxesCombined, CircleGauge, DatabaseZap, History, Newspaper, Settings, SlidersHorizontal } from 'lucide-react';
import { NavLink, Outlet } from 'react-router-dom';
import { api } from '../api';
import { useApi } from '../hooks';
import { StatusBadge } from './States';

const links = [
  ['/evidence', 'Evidence Journal', History],
  ['/', 'Dashboard', CircleGauge], ['/live', 'Live Market', Activity], ['/predictions', 'Predictions', Bot],
  ['/options', 'Option Chain', SlidersHorizontal], ['/futures', 'Futures', CandlestickChart], ['/models', 'Models', ChartNoAxesCombined],
  ['/history', 'History', History], ['/memory', 'Market Memory', BrainCircuit], ['/news', 'News & Events', Newspaper], ['/data-quality', 'Data Quality', DatabaseZap], ['/settings', 'System Readiness', Settings],
] as const;
export function Shell() {
  const health = useApi(api.health, 5000);
  return <div className="app-shell"><aside><div className="brand"><BarChart3 /><div><strong>AI BANKNIFTY</strong><small>Prediction workstation</small></div></div><nav>{links.map(([to,label,Icon]) => <NavLink key={to} to={to} end={to === '/'}><Icon size={18}/><span>{label}</span></NavLink>)}</nav><div className="guardrail">ANALYSIS ONLY<br/><small>No order execution</small></div></aside>
    <div className="workspace"><header className="topbar"><div><b>BANKNIFTY</b><span className="muted">IST · one-minute</span></div><div className="status-row"><StatusBadge label={`API ${health.data?.symfony ?? 'OFFLINE'}`} tone={health.data?.symfony === 'UP' ? 'good':'bad'}/><StatusBadge label={`PYTHON ${health.data?.python ?? 'UNKNOWN'}`} tone={health.data?.python === 'UP' ? 'good':'warn'}/><StatusBadge label={health.data?.marketCollector?.collectorMode ?? 'OFFLINE'} tone={health.data?.marketCollector?.connected ? 'good':'neutral'}/></div></header><main><Outlet /></main></div></div>;
}
