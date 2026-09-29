import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import {
  FolderTree, Folder, FileText, Loader2, RefreshCw, AlertCircle, ChevronDown,
  Search, Trash2, Plus, PenLine, Sparkles, Upload,
} from "lucide-react";
import { listInstalledSkills, readSkillFiles, deleteSkill, publishSkill } from "@/lib/api";
import type { InstalledSkill, SkillBundleFile } from "@/lib/api";
import { ModalShell } from "./ModalShell";
import { SkillFilesView } from "./SkillFilesView";
import { MenuItem } from "./MenuPanel";
import { WriteSkillModal } from "./WriteSkillModal";
import { UploadSkillModal } from "./UploadSkillModal";
import { inputStyle, btnStyle, menuPanel, applyCardHover } from "./styles";

export function ManageSkillsModal({
  onClose,
  onCreateWithAgent,
}: {
  onClose: () => void;
  onCreateWithAgent: () => void;
}) {
  const [selected, setSelected] = useState<InstalledSkill | null>(null);
  const [listEpoch, setListEpoch] = useState(0);

  return selected
    ? <SkillDetail
        key={selected.name}
        skill={selected}
        onBack={() => setSelected(null)}
        onUninstalled={() => { setListEpoch((n) => n + 1); setSelected(null); }}
        onClose={onClose}
      />
    : <SkillList
        key={listEpoch}
        onOpen={setSelected}
        onClose={onClose}
        onCreateWithAgent={onCreateWithAgent}
      />;
}

function SkillList({
  onOpen, onClose, onCreateWithAgent,
}: {
  onOpen: (s: InstalledSkill) => void;
  onClose: () => void;
  onCreateWithAgent: () => void;
}) {
  const [skills, setSkills] = useState<InstalledSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState<"write" | "upload" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSkills(await listInstalledSkills());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to list skills");
      setSkills([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter((s) =>
      s.name.toLowerCase().includes(q) ||
      (s.description ?? "").toLowerCase().includes(q));
  }, [skills, query]);

  const subtitle = loading
    ? "Loading…"
    : error
      ? "Could not read the runtime's skills"
      : query.trim() && filtered.length !== skills.length
        ? `${filtered.length} of ${skills.length} installed`
        : `${skills.length} installed on this runtime`;

  const closeAdding = () => { setAdding(null); void load(); };

  return (
    <>
    <ModalShell
      icon={FolderTree}
      title="Manage skills"
      subtitle={subtitle}
      width={640}
      onClose={onClose}
      headerActions={<NewSkillMenu onPick={(action) => {
        if (action === "agent") {
          onCreateWithAgent();
          return;
        }
        setAdding(action);
      }} />}
    >
      <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
        <div style={{ position: "relative", flex: 1 }}>
          <Search size={14} style={{
            position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)",
            color: "var(--lm-text-muted)", pointerEvents: "none",
          }} />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search installed skills…"
            className="lm-u-input"
            style={{ ...inputStyle, paddingLeft: 30 }}
          />
        </div>
        <button
          type="button"
          className="lm-u-btn"
          onClick={() => void load()}
          disabled={loading}
          title="Reload"
          aria-label="Reload"
          style={{
            width: 36, borderRadius: 9, display: "flex",
            alignItems: "center", justifyContent: "center", flexShrink: 0,
          }}
        ><RefreshCw size={14} className={loading ? "lm-spin-ico" : undefined} /></button>
      </div>

      {loading ? (
        <Centered><Loader2 size={18} className="lm-spin-ico" /> Loading skills…</Centered>
      ) : error ? (
        <Centered tone="error"><AlertCircle size={16} /> {error}</Centered>
      ) : skills.length === 0 ? (
        <Centered>No skills installed on this runtime yet.</Centered>
      ) : filtered.length === 0 ? (
        <Centered>{`No installed skill matches \u201C${query.trim()}\u201D.`}</Centered>
      ) : (
        <div>
          <div style={{ ...skillGridCols, ...listHeadStyle }}>
            <span>Skill</span>
            <span>Store</span>
            <span style={{ textAlign: "right" }}>Files</span>
            <span style={{ textAlign: "right" }}>Last updated</span>
          </div>
          {filtered.map((s) => <SkillRow key={s.name} skill={s} onOpen={() => onOpen(s)} />)}
        </div>
      )}
    </ModalShell>

    {adding === "write" && <WriteSkillModal onClose={closeAdding} />}
    {adding === "upload" && <UploadSkillModal onClose={closeAdding} />}
    </>
  );
}

// The same Write/Upload pair the composer's "+" menu offers, repeated in this header so an operator already looking at the installed list doesn't have to close the modal to add one.
function NewSkillMenu({ onPick }: { onPick: (a: "write" | "upload" | "agent") => void }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    // Capture phase + stopPropagation so Escape closes THIS menu without also reaching ModalShell's document-level handler and closing the whole modal.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  const run = (a: "write" | "upload" | "agent") => { setOpen(false); onPick(a); };

  return (
    <div ref={wrapRef} style={{ position: "relative", flexShrink: 0 }}>
      <button
        type="button"
        className="lm-u-btn"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Add a skill"
        style={{
          height: 30, padding: "0 9px", borderRadius: 8,
          background: open ? "var(--lm-card)" : "var(--lm-bg)",
          border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
          display: "inline-flex", alignItems: "center", gap: 5,
          fontSize: 11.5, fontWeight: 600, whiteSpace: "nowrap",
        }}
      >
        <Plus size={13} /> New <ChevronDown size={12} style={{ opacity: 0.7 }} />
      </button>

      {open && (
        <div role="menu" className="lm-pop" style={{ ...menuPanel, top: "calc(100% + 6px)", right: 0, minWidth: 208 }}>
          <MenuItem icon={Sparkles} label="Create with Agent" hint="plan it together in chat" onClick={() => run("agent")} />
          <MenuItem icon={PenLine} label="Write skill" hint="author a new SKILL.md" onClick={() => run("write")} />
          <MenuItem icon={Upload} label="Upload a skill" hint=".skill / .zip / .md from this computer" onClick={() => run("upload")} />
        </div>
      )}
    </div>
  );
}

function SkillRow({ skill, onOpen }: { skill: InstalledSkill; onOpen: () => void }) {
  const files = countFiles(skill);
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{
        ...skillGridCols,
        alignItems: "start", width: "100%", textAlign: "left",
        padding: "9px 12px", borderRadius: 10, cursor: "pointer",
        background: "var(--lm-card)", border: "1px solid var(--lm-border)",
        color: "var(--lm-text)", transition: "background 0.12s, border-color 0.12s",
        marginBottom: 6,
      }}
      onMouseEnter={(e) => applyCardHover(e.currentTarget, true)}
      onMouseLeave={(e) => applyCardHover(e.currentTarget, false)}
    >
      <span style={{ display: "flex", alignItems: "flex-start", gap: 9, minWidth: 0 }}>
        <Folder size={15} style={{ color: "var(--lm-amber)", flexShrink: 0, marginTop: 1 }} />
        <span style={{ minWidth: 0 }}>
          <span style={{
            display: "block", fontSize: 13, fontWeight: 600,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>/{skill.name}</span>
          {skill.description && (
            <span style={{
              display: "block", fontSize: 11, color: "var(--lm-text-dim)", marginTop: 2,
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>{skill.description}</span>
          )}
        </span>
      </span>
      <StoreAvailability availability={skill.store_availability} />
      <span style={{ fontSize: 12, color: "var(--lm-text)", textAlign: "right", whiteSpace: "nowrap", marginTop: 1 }}>
        {files}
      </span>
      <span
        style={{ fontSize: 12, color: "var(--lm-text)", textAlign: "right", whiteSpace: "nowrap", marginTop: 1 }}
        title={skill.updated_at ? new Date(skill.updated_at * 1000).toLocaleString() : undefined}
      >{formatUpdated(skill.updated_at)}</span>
    </button>
  );
}

const skillGridCols: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "minmax(0, 1fr) 76px 52px 96px",
  gap: 10,
};

function StoreAvailability({ availability }: { availability?: InstalledSkill["store_availability"] }) {
  const labels = {
    in_store: { text: "In store", color: "var(--lm-green)" },
    device_only: { text: "Robot only", color: "var(--lm-text-muted)" },
    unknown: { text: "Unknown", color: "var(--lm-amber)" },
  } as const;
  const label = availability ? labels[availability] : labels.unknown;
  return (
    <span
      title={availability === "unknown" || !availability
        ? "The skill store could not be checked."
        : availability === "in_store"
          ? "A matching skill is currently available in the skill store."
          : "No matching skill is currently available in the skill store."}
      style={{ fontSize: 11, color: label.color, whiteSpace: "nowrap", marginTop: 2 }}
    >{label.text}</span>
  );
}

const listHeadStyle: CSSProperties = {
  padding: "0 12px 6px", fontSize: 10, fontWeight: 600, letterSpacing: "0.04em",
  textTransform: "uppercase", color: "var(--lm-text-dim)",
};

function SkillDetail({
  skill, onBack, onUninstalled, onClose,
}: {
  skill: InstalledSkill;
  onBack: () => void;
  onUninstalled: () => void;
  onClose: () => void;
}) {
  const [files, setFiles] = useState<SkillBundleFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState("");
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");

  const publish = async () => { setPublishing(true); setPublishError(""); try { await publishSkill(skill.name); } catch (e) { setPublishError(e instanceof Error ? e.message : "Failed to publish"); } finally { setPublishing(false); } };

  const uninstall = async () => {
    setRemoving(true);
    setRemoveError("");
    try {
      await deleteSkill(skill.name);
      onUninstalled();
    } catch (e) {
      setRemoveError(e instanceof Error ? e.message : "Failed to uninstall");
      setConfirming(false);
    } finally {
      setRemoving(false);
    }
  };

  useEffect(() => {
    let alive = true;
    readSkillFiles(skill.name)
      .then((b) => { if (alive) setFiles(b.files ?? []); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to read the skill"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [skill.name]);

  return (
    <ModalShell
      icon={FileText}
      title={`/${skill.name}`}
      subtitle={skill.description || "Installed on this runtime"}
      width={940}
      onClose={onClose}
      onBack={onBack}
      bodyPadding={0}
      footer={
        <div style={{ display: "flex", alignItems: "center", gap: 12, width: "100%" }}>
          <div style={{ flex: 1, minWidth: 0, fontSize: 11, lineHeight: 1.45 }}>
            {removeError ? (
              <span style={{ color: "var(--lm-red)" }}>{removeError}</span>
            ) : confirming ? (
              <span style={{ color: "var(--lm-red)" }}>
                Delete <strong>/{skill.name}</strong> and all its files? This can't be undone.
              </span>
            ) : (
              <span style={{ color: "var(--lm-text-muted)" }}>
                Removes the skill from this runtime's skills dir.
              </span>
            )}
          </div>
          {confirming && !removing && (
            <button
              type="button" className="lm-u-btn" style={{ ...btnStyle, flexShrink: 0 }}
              onClick={() => setConfirming(false)}
            >Cancel</button>
          )}
          <button
            type="button"
            className="lm-u-btn"
            style={{
              ...btnStyle, flexShrink: 0,
              display: "inline-flex", alignItems: "center", gap: 6,
              borderColor: "var(--lm-red-glow)",
              color: "var(--lm-red)",
              background: confirming ? "var(--lm-red-dim)" : undefined,
              opacity: removing ? 0.6 : 1,
              cursor: removing ? "not-allowed" : "pointer",
            }}
            disabled={removing}
            onClick={() => (confirming ? void uninstall() : setConfirming(true))}
          >
            {removing
              ? <><Loader2 size={14} className="lm-spin-ico" /> Uninstalling…</>
              : confirming
                ? <><Trash2 size={14} /> Confirm uninstall</>
                : <><Trash2 size={14} /> Uninstall</>}
          </button>
          {skill.store_availability === "device_only" && <button type="button" className="lm-u-btn" style={btnStyle} disabled={publishing} onClick={() => void publish()}>{publishing ? "Publishing…" : "Publish to store"}</button>}
          {publishError && <span style={{ color: "var(--lm-red)", fontSize: 11 }}>{publishError}</span>}
        </div>
      }
    >
      {loading ? (
        <Filler><Loader2 size={16} className="lm-spin-ico" /> Reading files…</Filler>
      ) : error ? (
        <Filler tone="error"><AlertCircle size={16} /> {error}</Filler>
      ) : (
        <SkillFilesView files={files} />
      )}
    </ModalShell>
  );
}

// Counts files in the listing tree without a second request.
function countFiles(skill: InstalledSkill): number {
  let n = 0;
  const walk = (nodes: InstalledSkill["files"]) => {
    for (const node of nodes) {
      if (node.dir) walk(node.children ?? []);
      else n++;
    }
  };
  walk(skill.files ?? []);
  return n;
}

// Formats `updated_at` (Unix seconds) as fixed-width MM/DD/YYYY so the column aligns.
function formatUpdated(unixSeconds?: number): string {
  if (!unixSeconds) return "—";
  const d = new Date(unixSeconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}/${d.getFullYear()}`;
}

function Centered({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
      padding: "44px 12px", textAlign: "center",
      fontSize: 12.5, color: tone === "error" ? "var(--lm-red)" : "var(--lm-text-muted)",
    }}>{children}</div>
  );
}

// Filler fills the full-bleed body of the detail shell (bodyPadding={0}).
function Filler({ children, tone }: { children: React.ReactNode; tone?: "error" }) {
  return (
    <div style={{
      flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
      fontSize: 12.5, color: tone === "error" ? "var(--lm-red)" : "var(--lm-text-muted)",
    }}>{children}</div>
  );
}
