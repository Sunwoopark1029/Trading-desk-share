import { useCallback, useEffect, useRef, useState } from "react";

export interface PollState<T> {
  data: T | null;
  error: string | null; // last error message (data keeps the last good value)
  loading: boolean;
  lastOk: number | null; // ms epoch of the last successful load
  refresh: () => void;
}

/**
 * Call `fetcher` now and every `intervalMs`. Errors never clear the last good data.
 * Changing any value in `deps` drops the old data and starts over.
 */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const gen = useRef(0);
  const busy = useRef(false);

  const run = useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    const my = gen.current;
    try {
      const d = await fetchRef.current();
      if (my !== gen.current) return;
      setData(d);
      setError(null);
      setLastOk(Date.now());
    } catch (e) {
      if (my !== gen.current) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      busy.current = false;
      if (my === gen.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    gen.current += 1;
    busy.current = false;
    setData(null);
    setError(null);
    setLoading(true);
    setLastOk(null);
    void run();
    const id = window.setInterval(() => void run(), intervalMs);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, run, ...deps]);

  return { data, error, loading, lastOk, refresh: () => void run() };
}

/** Re-render every `ms` so "N s ago" labels stay current. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

export function useMediaQuery(query: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    mq.addEventListener("change", on);
    on();
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}
