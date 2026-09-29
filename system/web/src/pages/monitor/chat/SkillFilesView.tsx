import { useMemo, useState } from "react";
import { FileText } from "lucide-react";
import type { SkillBundleFile } from "@/lib/api";

export function SkillFilesView({
  files, skipped,
}: {
  files: SkillBundleFile[];
  /** Files dropped by a cap upstream, surfaced so the list never silently lies. */
  skipped?: number;
}) {
  const [picked, setPicked] = useState<string | null>(null);

  const fallback = useMemo(
    () => files.find((f) => f.path.toLowerCase().endsWith("skill.md")) ?? files[0],
    [files],
  );
  const activeFile = files.find((f) => f.path === picked) ?? fallback ?? null;
  const active = activeFile?.path ?? null;
  const setActive = setPicked;

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 0, minWidth: 0 }}>
      <div style={{
        width: 250, flexShrink: 0, overflowY: "auto",
        borderRight: "1px solid var(--lm-border)", background: "var(--lm-bg)",
        padding: 8,
      }}>
        {files.map((f) => (
          <FileRow key={f.path} file={f} active={f.path === active} onClick={() => setActive(f.path)} />
        ))}
        {(skipped ?? 0) > 0 && (
          <div style={{ fontSize: 10, color: "var(--lm-text-muted)", padding: "6px 8px" }}>
            +{skipped} more file(s) not shown
          </div>
        )}
      </div>

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {activeFile ? (
          <>
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
              padding: "8px 14px", borderBottom: "1px solid var(--lm-border)", flexShrink: 0,
            }}>
              <span style={{
                fontSize: 11, color: "var(--lm-text-dim)", fontFamily: "monospace",
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>{activeFile.path}</span>
              <span style={{ fontSize: 10, color: "var(--lm-text-muted)", flexShrink: 0 }}>
                {formatBytes(activeFile.size)}
              </span>
            </div>
            <div style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 14 }}>
              {activeFile.binary ? (
                <div style={{ fontSize: 12, color: "var(--lm-text-muted)" }}>
                  Binary file — no preview.
                </div>
              ) : (
                <pre style={{
                  margin: 0, fontSize: 11.5, lineHeight: 1.6,
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  color: "var(--lm-text)", whiteSpace: "pre-wrap", wordBreak: "break-word",
                }}>{activeFile.text}</pre>
              )}
              {activeFile.truncated && (
                <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 10 }}>
                  … truncated — this preview shows the first 512 KB.
                </div>
              )}
            </div>
          </>
        ) : (
          <div style={{
            flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 12.5, color: "var(--lm-text-muted)",
          }}>{files.length === 0 ? "This skill has no files." : "Select a file"}</div>
        )}
      </div>
    </div>
  );
}

function FileRow({
  file, active, onClick,
}: {
  file: SkillBundleFile;
  active: boolean;
  onClick: () => void;
}) {
  const slash = file.path.lastIndexOf("/");
  const dir = slash >= 0 ? file.path.slice(0, slash + 1) : "";
  const base = slash >= 0 ? file.path.slice(slash + 1) : file.path;

  return (
    <button
      type="button"
      onClick={onClick}
      title={file.path}
      style={{
        display: "flex", alignItems: "center", gap: 7, width: "100%", textAlign: "left",
        padding: "6px 8px", borderRadius: 7, border: "none", cursor: "pointer",
        background: active ? "color-mix(in srgb, var(--lm-amber) 12%, transparent)" : "transparent",
        color: active ? "var(--lm-text)" : "var(--lm-text-dim)",
        transition: "background 0.12s",
      }}
      onMouseEnter={(e) => { if (!active) e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 6%, transparent)"; }}
      onMouseLeave={(e) => { if (!active) e.currentTarget.style.background = "transparent"; }}
    >
      <FileText size={13} style={{
        color: active ? "var(--lm-amber)" : "var(--lm-text-muted)",
        flexShrink: 0, alignSelf: "flex-start", marginTop: 2,
      }} />
      <span style={{ minWidth: 0, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11 }}>
        {dir && (
          <span style={{
            display: "block", fontSize: 9.5, color: "var(--lm-text-muted)",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>{dir}</span>
        )}
        <span style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{base}</span>
      </span>
    </button>
  );
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
