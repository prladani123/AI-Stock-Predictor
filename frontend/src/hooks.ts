import { useCallback, useEffect, useState } from 'react';
export function useApi<T>(loader: () => Promise<T>, pollMs?: number) {
  const [data, setData] = useState<T | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => { setLoading(true); try { setData(await loader()); setError(null); } catch (value) { setError(value instanceof Error ? value.message : 'Unexpected application error.'); } finally { setLoading(false); } }, [loader]);
  useEffect(() => { void refresh(); if (!pollMs) return; const timer = window.setInterval(() => void refresh(), pollMs); return () => clearInterval(timer); }, [refresh, pollMs]);
  return {data, loading, error, refresh, setData};
}
export const formatIst = (value?: string | null) => value ? new Intl.DateTimeFormat('en-IN', {timeZone: 'Asia/Kolkata', dateStyle: 'medium', timeStyle: 'short'}).format(new Date(value)) : '-';
export const formatNumber = (value?: number | null, digits = 2) => value == null ? '-' : value.toLocaleString('en-IN', {minimumFractionDigits: digits, maximumFractionDigits: digits});
