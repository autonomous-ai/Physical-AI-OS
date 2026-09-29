import { useRef, useState } from "react";
import { Upload, Check, AlertCircle, Loader2, FileArchive, X } from "lucide-react";
import { uploadSkill } from "@/lib/api";
import { ModalShell } from "./ModalShell";
import { btnStyle } from "./styles";

// Matches the device-side cap in skills.StoreMaxBytes.
const MAX_BYTES = 16 * 1024 * 1024;

const ACCEPT = ".skill,.zip,.md,application/zip,text/markdown";

export function UploadSkillModal({ onClose }: { onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const [installed, setInstalled] = useState<{ name: string; path: string } | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const pick = (f: File | undefined) => {
    if (!f) return;
    if (f.size === 0) {
      setError("That file is empty.");
      return;
    }
    if (f.size > MAX_BYTES) {
      setError(`That archive is ${formatMB(f.size)} — the limit is ${formatMB(MAX_BYTES)}.`);
      return;
    }
    setError("");
    setFile(f);
  };

  const submit = async () => {
    if (!file) return;
    setUploading(true);
    setError("");
    try {
      setInstalled(await uploadSkill(file));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
    <ModalShell
      icon={Upload}
      title="Upload a skill"
      subtitle="Install a skill from this computer"
      width={560}
      onClose={onClose}
      footer={installed ? (
        <button type="button" className="lm-u-btn lm-u-btn-primary" style={btnStyle} onClick={onClose}>Done</button>
      ) : (
        <>
          <button type="button" className="lm-u-btn" style={btnStyle} onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="lm-u-btn lm-u-btn-primary"
            style={{
              ...btnStyle, display: "inline-flex", alignItems: "center", gap: 6,
              opacity: file && !uploading ? 1 : 0.5,
              cursor: file && !uploading ? "pointer" : "not-allowed",
            }}
            disabled={!file || uploading}
            onClick={() => void submit()}
          >
            {uploading
              ? <><Loader2 size={14} className="lm-spin-ico" /> Installing…</>
              : <><Upload size={14} /> Install</>}
          </button>
        </>
      )}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        style={{ display: "none" }}
        onChange={(e) => pick(e.target.files?.[0])}
      />

      {installed ? (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, padding: "36px 12px", textAlign: "center" }}>
          <Check size={28} style={{ color: "var(--lm-amber)" }} />
          <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--lm-text)" }}>
            /{installed.name} installed
          </div>
          <div style={{
            fontSize: 10.5, color: "var(--lm-text-muted)",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", wordBreak: "break-all",
          }}>{installed.path}</div>
          <div style={{ fontSize: 11, color: "var(--lm-text-dim)", marginTop: 4, maxWidth: 380, lineHeight: 1.5 }}>
            The agent picks it up on its next session — no restart needed.
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pick(e.dataTransfer.files?.[0]);
            }}
            style={{
              display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
              gap: 8, padding: "30px 16px", borderRadius: 12, cursor: "pointer", textAlign: "center",
              border: `1px dashed ${dragging ? "var(--lm-amber)" : "var(--lm-border-hi)"}`,
              background: dragging ? "color-mix(in srgb, var(--lm-amber) 8%, transparent)" : "var(--lm-bg)",
              transition: "border-color 0.15s, background 0.15s",
            }}
          >
            <Upload size={20} style={{ color: dragging ? "var(--lm-amber)" : "var(--lm-text-muted)" }} />
            <div style={{ fontSize: 12.5, color: "var(--lm-text)" }}>
              Drop a <strong>.md</strong>, <strong>.zip</strong> or <strong>.skill</strong> here, or click to choose
            </div>
            <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)" }}>
              Max {formatMB(MAX_BYTES)}
            </div>
          </div>

          {file && (
            <div style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "10px 12px", borderRadius: 10,
              background: "var(--lm-card)", border: "1px solid var(--lm-border)",
            }}>
              <FileArchive size={16} style={{ color: "var(--lm-amber)", flexShrink: 0, alignSelf: "flex-start", marginTop: 2 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{
                  fontSize: 12.5, color: "var(--lm-text)",
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>{file.name}</div>
                <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginTop: 2 }}>
                  {formatMB(file.size)}
                </div>
              </div>
              <button
                type="button"
                onClick={() => { setFile(null); if (inputRef.current) inputRef.current.value = ""; }}
                aria-label="Remove file"
                style={{
                  background: "transparent", border: "none", cursor: "pointer",
                  color: "var(--lm-text-muted)", padding: 4, borderRadius: 4,
                  display: "flex", alignItems: "center", flexShrink: 0,
                }}
              ><X size={14} /></button>
            </div>
          )}

          <div style={{
            fontSize: 11, color: "var(--lm-text-dim)", lineHeight: 1.6,
            padding: "10px 12px", borderRadius: 9,
            background: "var(--lm-bg)", border: "1px solid var(--lm-border)",
          }}>
            <div style={{ fontWeight: 600, color: "var(--lm-text)", marginBottom: 4 }}>
              File requirements
            </div>
            {/* Tailwind preflight strips list markers, so request the disc explicitly. */}
            <ul style={{ margin: 0, paddingLeft: 16, listStyle: "disc outside" }}>
              <li style={{ display: "list-item", marginBottom: 3 }}>
                A <strong>.md</strong> file must carry the skill <strong>name</strong> and{" "}
                <strong>description</strong> in YAML front-matter — that name is what it installs as.
              </li>
              <li style={{ display: "list-item" }}>
                A <strong>.zip</strong> or <strong>.skill</strong> file must include a{" "}
                <strong>SKILL.md</strong> at the skill root; its top-level folder names the skill.
              </li>
            </ul>
            <div style={{ marginTop: 6 }}>
              Installing <strong>replaces</strong> an existing skill of the same name.{" "}
              <a
                href="https://github.com/anthropics/skills/tree/main/skills/algorithmic-art"
                target="_blank"
                rel="noreferrer noopener"
                style={{ color: "var(--lm-amber)" }}
              >See an example</a>.
            </div>
          </div>

          {error && (
            <div style={{
              display: "flex", alignItems: "flex-start", gap: 7,
              fontSize: 11.5, color: "var(--lm-red)",
              background: "var(--lm-red-dim)",
              border: "1px solid var(--lm-red-glow)",
              borderRadius: 9, padding: "8px 10px",
            }}>
              <AlertCircle size={13} style={{ flexShrink: 0, marginTop: 1 }} /> <span>{error}</span>
            </div>
          )}
        </div>
      )}
    </ModalShell>
  );
}

function formatMB(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
