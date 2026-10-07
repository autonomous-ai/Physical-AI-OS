import type { CSSProperties, Dispatch, SetStateAction } from "react";
import { Mic, MicOff } from "lucide-react";
import { hwUrl } from "@/lib/api";
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
        <h2 className="lm-users-card-title"><Mic size={17} aria-hidden />Unknown voices</h2>
        <span style={{ fontSize: 12, color: "var(--lm-text-muted)" }}>
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
        <div style={{ display: "flex", flexDirection: "column", gap: 10, maxHeight: 320, overflowY: "auto" }} className="lm-users-list">
          {strangers.clusters.map((cluster) => {
            const isOpen = expandedCluster[cluster.hash] ?? false;
            return (
              <div key={cluster.hash} style={{
                padding: "12px",
                borderRadius: 6,
                background: "var(--lm-surface)",
                border: "1px solid var(--lm-border)",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <button
                    className="lm-voice-cluster-toggle"
                    aria-expanded={isOpen}
                    aria-label={`${isOpen ? "Collapse" : "Expand"} voice cluster ${cluster.hash}`}
                    onClick={() => setExpandedCluster((p) => ({ ...p, [cluster.hash]: !isOpen }))}
                  >
                    <span style={{ color: "var(--lm-purple)", fontSize: 12 }}>{isOpen ? "▾" : "▸"}</span>
                    <span style={{ fontSize: 12, fontWeight: 600, color: "var(--lm-purple)", fontFamily: "monospace", overflowWrap: "anywhere" }}>
                      {cluster.hash}
                    </span>
                    <span style={{ fontSize: 12, color: "var(--lm-purple)", fontWeight: 600 }}>
                      ×{cluster.sample_count}
                    </span>
                    <span style={{ fontSize: 12, color: "var(--lm-text-muted)" }}>
                      · {fmtAgo(cluster.latest_mtime)}
                    </span>
                  </button>
                  <button
                    className="lm-users-delete"
                    disabled={deletingCluster === cluster.hash}
                    aria-label={`Delete cluster ${cluster.hash}`}
                    onClick={(e) => { e.stopPropagation(); if (deletingCluster !== cluster.hash) onDeleteCluster(cluster.hash, cluster.sample_count); }}
                    title={`Delete cluster ${cluster.hash}`}
                    style={{
                      cursor: deletingCluster === cluster.hash ? "wait" : "pointer",
                      fontSize: 12, color: "var(--lm-red)",
                      opacity: deletingCluster === cluster.hash ? 0.5 : 0.7,
                      fontWeight: 600, flexShrink: 0, padding: "0 4px",
                    }}
                  >
                    {deletingCluster === cluster.hash ? "…" : "✕"}
                  </button>
                </div>

                {isOpen && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 6 }}>
                    {cluster.samples.map((s) => {
                      const fileKey = `${cluster.hash}/${s.filename}`;
                      const isDeletingFile = deletingStrangerFile === fileKey;
                      return (
                        <div key={s.filename} title={s.filename} style={{
                          display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8,
                          fontSize: 12, color: "var(--lm-text-muted)", fontFamily: "monospace",
                        }}>
                          <audio
                            controls preload="none"
                            src={hwUrl(`/voice/strangers/audio/${encodeURIComponent(cluster.hash)}/${encodeURIComponent(s.filename)}`)}
                            style={{ height: 36, width: "100%", minWidth: 0 }}
                          />
                          <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {s.filename}
                          </span>
                          <span style={{ flexShrink: 0, fontSize: 12 }}>
                            {fmtSize(s.size_bytes)} · {fmtAgo(s.mtime)}
                          </span>
                          <button
                            className="lm-users-delete"
                            disabled={isDeletingFile}
                            aria-label={`Remove ${s.filename}`}
                            onClick={() => { if (!isDeletingFile) onDeleteStrangerFile(cluster.hash, s.filename); }}
                            title={`Remove ${s.filename}`}
                            style={{
                              cursor: isDeletingFile ? "wait" : "pointer",
                              fontSize: 12, color: "var(--lm-red)",
                              opacity: isDeletingFile ? 0.5 : 0.7,
                              fontWeight: 600, flexShrink: 0, padding: "0 2px",
                            }}
                          >✕</button>
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
