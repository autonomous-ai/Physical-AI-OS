import type { CSSProperties, Dispatch, SetStateAction } from "react";
import { Mic, MicOff } from "lucide-react";
import { hwUrl } from "@/lib/api";
import { CardLabel } from "../components";
import { EmptyState } from "./EmptyState";
import { fmtAgo, fmtSize } from "./format";
import type { StrangersData } from "./types";

// Unknown Voice Clusters.
export function StrangerClustersCard({
  strangers, strangersError, expandedCluster, setExpandedCluster,
  deletingCluster, deletingStrangerFile, onDeleteCluster, onDeleteStrangerFile,
  monCard, cardHeader,
}: {
  strangers: StrangersData | null;
  strangersError: boolean;
  expandedCluster: Record<string, boolean>;
  setExpandedCluster: Dispatch<SetStateAction<Record<string, boolean>>>;
  deletingCluster: string | null;
  deletingStrangerFile: string | null;
  onDeleteCluster: (hash: string, sampleCount: number) => void;
  onDeleteStrangerFile: (hash: string, filename: string) => void;
  monCard: CSSProperties;
  cardHeader: CSSProperties;
}) {
  return (
    <div className="lm-mon-card" style={monCard}>
      <div style={cardHeader}>
        <CardLabel icon={<Mic size={13} />} text="Unknown Voices" />
        <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
          {strangers ? `${strangers.total} cluster${strangers.total !== 1 ? "s" : ""}` : ""}
        </span>
      </div>

      {strangersError && (
        <EmptyState icon={<MicOff size={18} />} text="Voice cluster info unavailable (speaker service down?)" />
      )}

      {!strangersError && strangers && strangers.clusters.length === 0 && (
        <EmptyState icon={<Mic size={18} />} text="No unknown voices heard yet." />
      )}

      {!strangersError && strangers && strangers.clusters.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 320, overflowY: "auto" }} className="lm-hide-scroll lm-scroll-fade">
          {strangers.clusters.map((cluster) => {
            const isOpen = expandedCluster[cluster.hash] ?? false;
            return (
              <div key={cluster.hash} style={{
                padding: "5px 9px",
                borderRadius: 6,
                background: "var(--lm-surface)",
                border: "1px solid var(--lm-border)",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", flex: 1, minWidth: 0 }}
                    onClick={() => setExpandedCluster((p) => ({ ...p, [cluster.hash]: !isOpen }))}
                  >
                    <span style={{ color: "var(--lm-purple)", fontSize: 10 }}>{isOpen ? "▾" : "▸"}</span>
                    <span style={{ fontSize: 12, fontWeight: 600, color: "var(--lm-purple)", fontFamily: "monospace" }}>
                      {cluster.hash}
                    </span>
                    <span style={{ fontSize: 10, color: "var(--lm-purple)", fontWeight: 600 }}>
                      ×{cluster.sample_count}
                    </span>
                    <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>
                      · {fmtAgo(cluster.latest_mtime)}
                    </span>
                  </div>
                  <span
                    onClick={(e) => { e.stopPropagation(); if (deletingCluster !== cluster.hash) onDeleteCluster(cluster.hash, cluster.sample_count); }}
                    title={`Delete cluster ${cluster.hash}`}
                    style={{
                      cursor: deletingCluster === cluster.hash ? "wait" : "pointer",
                      fontSize: 11, color: "var(--lm-red)",
                      opacity: deletingCluster === cluster.hash ? 0.5 : 0.7,
                      fontWeight: 600, flexShrink: 0, padding: "0 4px",
                    }}
                  >
                    {deletingCluster === cluster.hash ? "…" : "✕"}
                  </span>
                </div>

                {isOpen && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 6 }}>
                    {cluster.samples.map((s) => {
                      const fileKey = `${cluster.hash}/${s.filename}`;
                      const isDeletingFile = deletingStrangerFile === fileKey;
                      return (
                        <div key={s.filename} title={s.filename} style={{
                          display: "flex", alignItems: "center", gap: 6,
                          fontSize: 10, color: "var(--lm-text-muted)", fontFamily: "monospace",
                        }}>
                          <audio
                            controls preload="none"
                            src={hwUrl(`/voice/strangers/audio/${encodeURIComponent(cluster.hash)}/${encodeURIComponent(s.filename)}`)}
                            style={{ height: 22, flexShrink: 0, width: 180 }}
                          />
                          <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {s.filename}
                          </span>
                          <span style={{ flexShrink: 0, fontSize: 9 }}>
                            {fmtSize(s.size_bytes)} · {fmtAgo(s.mtime)}
                          </span>
                          <span
                            onClick={() => { if (!isDeletingFile) onDeleteStrangerFile(cluster.hash, s.filename); }}
                            title={`Remove ${s.filename}`}
                            style={{
                              cursor: isDeletingFile ? "wait" : "pointer",
                              fontSize: 11, color: "var(--lm-red)",
                              opacity: isDeletingFile ? 0.5 : 0.7,
                              fontWeight: 600, flexShrink: 0, padding: "0 2px",
                            }}
                          >✕</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
