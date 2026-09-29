// Kept out of components.tsx so that file only exports components (Fast Refresh).
import { useEffect, useRef, useState } from "react";

// useCountUp animates a number from its previous value to `target` over ~`ms`, so live stats (Mbps, volume %) tick up instead of snapping.
export function useCountUp(target: number, ms = 600): number {
  const [display, setDisplay] = useState(target);
  const fromRef = useRef(target);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const from = fromRef.current;
    if (from === target) return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const start = performance.now();
    const tick = (t: number) => {
      const p = reduce ? 1 : Math.min(1, (t - start) / ms);
      const eased = 1 - Math.pow(1 - p, 3);
      setDisplay(Math.round(from + (target - from) * eased));
      if (p < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        fromRef.current = target;
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
  }, [target, ms]);

  return display;
}

export function formatUptime(s: number) {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// formatAgo turns a "seconds since X" count into a compact human string ("just now", "42s ago", "29m ago", "2h ago").
export function formatAgo(s: number): string {
  if (s < 0) return "—";
  if (s < 5) return "just now";
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// formatSize converts a value in `unit` (KB or MB) to a human-readable string, promoting to MB/GB/TB as needed.
export function formatSize(value: number, unit: "KB" | "MB"): string {
  if (!value || value < 0) return "—";
  let bytes = unit === "KB" ? value * 1024 : value * 1024 * 1024;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024;
    i++;
  }
  return i >= 3 ? `${bytes.toFixed(1)} ${units[i]}` : `${Math.round(bytes)} ${units[i]}`;
}
