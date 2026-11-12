import type { ReactNode } from 'react';
export function Panel({title, action, children, className = ''}: {title?: string; action?: ReactNode; children: ReactNode; className?: string}) { return <section className={`panel ${className}`}>{(title || action) && <header className="panel-head"><h2>{title}</h2>{action}</header>}{children}</section>; }
export function StatusBadge({label, tone = 'neutral'}: {label: string; tone?: 'good'|'bad'|'warn'|'neutral'}) { return <span className={`badge ${tone}`}><i />{label}</span>; }
export function EmptyState({title, detail}: {title: string; detail: string}) { return <div className="state"><strong>{title}</strong><span>{detail}</span></div>; }
export function LoadingState() { return <div className="state"><span className="spinner" />Loading market state...</div>; }
export function ErrorState({message, retry}: {message: string; retry?: () => void}) { return <div className="state error"><strong>Data unavailable</strong><span>{message}</span>{retry && <button onClick={retry}>Retry</button>}</div>; }
export function MetricCard({label, value, detail}: {label: string; value: ReactNode; detail?: ReactNode}) { return <article className="metric"><span>{label}</span><strong>{value}</strong>{detail && <small>{detail}</small>}</article>; }
