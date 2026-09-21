import { useCallback, useEffect, useRef, useState } from "react";

// Polling data hook — the UI's "realtime": refetch every `intervalMs`,
// pause when the tab is hidden.
export function usePoll<T>(fn: () => Promise<T>, deps: any[], intervalMs = 3000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const reload = useCallback(async () => {
    try {
      const result = await fnRef.current();
      setData(result);
      setError(null);
    } catch (e: any) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    reload();
    const timer = setInterval(() => {
      if (!document.hidden) reload();
    }, intervalMs);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, reload };
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 0) {
    const f = -s;
    if (f < 3600) return `in ${Math.ceil(f / 60)}m`;
    if (f < 86400) return `in ${Math.round(f / 3600)}h`;
    return `in ${Math.round(f / 86400)}d`;
  }
  if (s < 60) return `${Math.max(0, Math.floor(s))}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function duration(sec: number | null): string {
  if (sec == null) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.floor(sec % 60)}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}
