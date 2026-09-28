import { useCallback, useState } from "react";
import { HW } from "../types";
import { usePolling } from "../../../hooks/usePolling";
import type { StrangersData, FaceStrangerStat } from "./types";

// Unknown voice clusters (/voice/strangers) + face stranger visit stats (/face/stranger-stats), with their own polling and delete flows.
export function useStrangers() {
  const [strangers, setStrangers] = useState<StrangersData | null>(null);
  const [strangersError, setStrangersError] = useState(false);
  const [expandedCluster, setExpandedCluster] = useState<Record<string, boolean>>({});
  const [deletingCluster, setDeletingCluster] = useState<string | null>(null);
  const [deletingStrangerFile, setDeletingStrangerFile] = useState<string | null>(null);

  const [faceStrangers, setFaceStrangers] = useState<FaceStrangerStat[] | null>(null);
  const [faceStrangersError, setFaceStrangersError] = useState(false);

  const [confirmCluster, setConfirmCluster] = useState<{ hash: string; sampleCount: number } | null>(null);
  const [confirmStrangerFile, setConfirmStrangerFile] = useState<{ hash: string; filename: string } | null>(null);

  const refreshStrangers = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch(`${HW}/voice/strangers`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = await res.json();
      setStrangers({
        total: j.total ?? 0,
        clusters: Array.isArray(j.clusters) ? j.clusters : [],
      });
      setStrangersError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setStrangersError(true);
    }
  }, []);

  usePolling(async (signal) => { await refreshStrangers(signal); }, 15_000);

  const refreshFaceStrangers = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await fetch(`${HW}/face/stranger-stats`, { signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = (await res.json()) as Record<string, { count?: number; first_seen?: string; last_seen?: string }>;
      const rows: FaceStrangerStat[] = Object.entries(j ?? {}).map(([sid, v]) => ({
        stranger_id: sid,
        count: v?.count ?? 0,
        first_seen: v?.first_seen ?? "",
        last_seen: v?.last_seen ?? "",
      }));
      rows.sort((a, b) => Date.parse(b.last_seen || "") - Date.parse(a.last_seen || ""));
      setFaceStrangers(rows);
      setFaceStrangersError(false);
    } catch (e) {
      if ((e as Error).name === "AbortError") return;
      setFaceStrangersError(true);
    }
  }, []);

  usePolling(async (signal) => { await refreshFaceStrangers(signal); }, 15_000);

  // Open the themed confirm dialog for deleting a stranger voice cluster.
  const handleDeleteCluster = (hash: string, sampleCount: number) => setConfirmCluster({ hash, sampleCount });

  const confirmDeleteCluster = async () => {
    if (!confirmCluster) return;
    const { hash } = confirmCluster;
    setConfirmCluster(null);
    setDeletingCluster(hash);
    try {
      const res = await fetch(`${HW}/voice/strangers/${hash}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        alert(`Delete failed: ${err.detail ?? res.status}`);
      }
      await refreshStrangers();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    } finally {
      setDeletingCluster(null);
    }
  };

  // Open the themed confirm dialog for deleting a single stranger sample file.
  const handleDeleteStrangerFile = (hash: string, filename: string) => setConfirmStrangerFile({ hash, filename });

  const confirmDeleteStrangerFile = async () => {
    if (!confirmStrangerFile) return;
    const { hash, filename } = confirmStrangerFile;
    setConfirmStrangerFile(null);
    const key = `${hash}/${filename}`;
    setDeletingStrangerFile(key);
    try {
      const res = await fetch(`${HW}/voice/strangers/${hash}/${encodeURIComponent(filename)}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
        alert(`Delete failed: ${err.detail ?? res.status}`);
      }
      await refreshStrangers();
    } catch (e) {
      alert(`Delete failed: ${(e as Error).message}`);
    } finally {
      setDeletingStrangerFile(null);
    }
  };

  return {
    strangers, strangersError,
    expandedCluster, setExpandedCluster,
    deletingCluster, deletingStrangerFile,
    faceStrangers, faceStrangersError,
    confirmCluster, setConfirmCluster,
    confirmStrangerFile, setConfirmStrangerFile,
    handleDeleteCluster, confirmDeleteCluster,
    handleDeleteStrangerFile, confirmDeleteStrangerFile,
  };
}
