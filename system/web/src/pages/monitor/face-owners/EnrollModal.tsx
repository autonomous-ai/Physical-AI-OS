import type { CSSProperties, RefObject } from "react";
import { createPortal } from "react-dom";
import { UserPlus, X, AtSign, Hash, ImagePlus, Loader2 } from "lucide-react";

// Add New User modal.
export function EnrollModal({
  themeClass,
  enrollName, setEnrollName,
  enrollTgUsername, setEnrollTgUsername,
  enrollTgId, setEnrollTgId,
  enrollFile, setEnrollFile,
  enrollPreview,
  enrolling, enrollError,
  enrollDragging, setEnrollDragging,
  fileInputRef,
  onClose, onSubmit,
  inputStyle, fieldLabel, btnStyle,
}: {
  themeClass: string;
  enrollName: string; setEnrollName: (v: string) => void;
  enrollTgUsername: string; setEnrollTgUsername: (v: string) => void;
  enrollTgId: string; setEnrollTgId: (v: string) => void;
  enrollFile: File | null; setEnrollFile: (f: File | null) => void;
  enrollPreview: string | null;
  enrolling: boolean; enrollError: string;
  enrollDragging: boolean; setEnrollDragging: (v: boolean) => void;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onClose: () => void; onSubmit: () => void;
  inputStyle: CSSProperties; fieldLabel: CSSProperties; btnStyle: CSSProperties;
}) {
  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)", backdropFilter: "blur(4px)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Add new user"
        className="lm-pop"
        style={{
          width: "min(440px, 100%)", maxHeight: "90vh",
          display: "flex", flexDirection: "column",
          background: "linear-gradient(180deg, color-mix(in srgb, var(--lm-amber) 4%, transparent), transparent 130px), var(--lm-surface)",
          border: "1px solid var(--lm-border-hi)",
          borderRadius: 14, boxShadow: "0 24px 64px -20px rgba(0,0,0,0.7), 0 2px 8px rgba(0,0,0,0.4)",
          overflow: "hidden",
        }}
      >
        <div style={{
          flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 12, padding: "16px 18px", borderBottom: "1px solid var(--lm-border)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <UserPlus size={16} style={{ color: "var(--lm-amber)" }} />
            <span style={{ fontSize: 14.5, fontWeight: 600, color: "var(--lm-text)" }}>Add New User</span>
          </div>
          <button
            type="button" onClick={onClose} aria-label="Close"
            className="lm-u-btn"
            style={{
              width: 30, height: 30, borderRadius: 8, background: "var(--lm-bg)",
              border: "1px solid var(--lm-border)", color: "var(--lm-text-dim)", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <X size={15} />
          </button>
        </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: "18px", overflowY: "auto" }}>
        <div>
          <label htmlFor="enroll-name" style={fieldLabel}>
            Name <span style={{ color: "var(--lm-amber)" }}>*</span>
          </label>
          <input
            id="enroll-name"
            type="text"
            placeholder="e.g. Leo"
            value={enrollName}
            onChange={(e) => setEnrollName(e.target.value)}
            className="lm-u-input"
            style={inputStyle}
          />
        </div>

        <div>
          <label style={fieldLabel}>Telegram <span style={{ fontWeight: 400, textTransform: "none", letterSpacing: 0, color: "var(--lm-text-muted)" }}>· optional</span></label>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ position: "relative" }}>
              <AtSign size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--lm-text-muted)", pointerEvents: "none" }} />
              <input
                type="text"
                placeholder="username"
                value={enrollTgUsername}
                onChange={(e) => setEnrollTgUsername(e.target.value)}
                className="lm-u-input"
                style={{ ...inputStyle, paddingLeft: 30 }}
              />
            </div>
            <div style={{ position: "relative" }}>
              <Hash size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "var(--lm-text-muted)", pointerEvents: "none" }} />
              <input
                type="text"
                placeholder="chat ID"
                value={enrollTgId}
                onChange={(e) => setEnrollTgId(e.target.value)}
                className="lm-u-input"
                style={{ ...inputStyle, paddingLeft: 30 }}
              />
            </div>
          </div>
        </div>

        <div>
          <label style={fieldLabel}>
            Photo <span style={{ color: "var(--lm-amber)" }}>*</span>
          </label>
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            onChange={(e) => { setEnrollFile(e.target.files?.[0] ?? null); e.target.value = ""; }}
            style={{ display: "none" }}
          />
          <div style={{ display: "flex", alignItems: "stretch", gap: 10 }}>
            {enrollPreview && (
              <div style={{ position: "relative", width: 60, height: 60, flexShrink: 0 }}>
                <img
                  src={enrollPreview}
                  alt=""
                  style={{ width: 60, height: 60, objectFit: "cover", borderRadius: 8, border: "1px solid var(--lm-border)", display: "block" }}
                />
                <button
                  type="button"
                  onClick={() => setEnrollFile(null)}
                  aria-label="Remove photo"
                  style={{
                    position: "absolute", top: -6, right: -6, width: 20, height: 20, borderRadius: "50%",
                    background: "var(--lm-bg)", border: "1px solid var(--lm-border)", color: "var(--lm-red)",
                    cursor: "pointer", padding: 0, display: "flex", alignItems: "center", justifyContent: "center",
                  }}
                >
                  <X size={12} />
                </button>
              </div>
            )}
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setEnrollDragging(true); }}
              onDragLeave={(e) => { e.preventDefault(); setEnrollDragging(false); }}
              onDrop={(e) => {
                e.preventDefault();
                setEnrollDragging(false);
                const f = e.dataTransfer.files?.[0];
                if (f && f.type.startsWith("image/")) setEnrollFile(f);
              }}
              className="lm-face-drop"
              style={{
                flex: 1, minWidth: 0, boxSizing: "border-box", cursor: "pointer",
                display: "flex", alignItems: "center", gap: 11,
                minHeight: enrollPreview ? 60 : 56, padding: "10px 12px 10px 14px", borderRadius: 10,
                border: `1.5px dashed ${enrollDragging || enrollFile ? "var(--lm-amber-glow)" : "var(--lm-border)"}`,
                background: enrollDragging || enrollFile ? "var(--lm-amber-dim)" : "var(--lm-bg)",
                color: enrollDragging || enrollFile ? "var(--lm-amber)" : "var(--lm-text-dim)",
              }}
            >
              <span style={{
                flexShrink: 0, width: 34, height: 34, borderRadius: 8,
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                background: enrollDragging || enrollFile ? "var(--lm-amber-dim)" : "var(--lm-surface)",
                border: `1px solid ${enrollDragging || enrollFile ? "var(--lm-amber-glow)" : "var(--lm-border)"}`,
                color: enrollDragging || enrollFile ? "var(--lm-amber)" : "var(--lm-text-dim)",
              }}>
                <ImagePlus size={17} />
              </span>
              <span style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 2, textAlign: "left" }}>
                <span style={{ fontSize: 12.5, fontWeight: enrollFile ? 600 : 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: enrollDragging || enrollFile ? "var(--lm-amber)" : "var(--lm-text)" }}>
                  {enrollFile ? enrollFile.name : (enrollDragging ? "Drop photo here" : "Choose a photo")}
                </span>
                {!enrollFile && !enrollDragging && (
                  <span style={{ fontSize: 10.5, color: "var(--lm-text-muted)" }}>or drag it here</span>
                )}
              </span>
              <span style={{
                flexShrink: 0, fontSize: 11, fontWeight: 600, padding: "5px 11px", borderRadius: 7,
                background: "var(--lm-amber-dim)", border: "1px solid var(--lm-amber-glow)",
                color: "var(--lm-amber)",
              }}>
                {enrollFile ? "Change" : "Browse"}
              </span>
            </button>
          </div>
        </div>

        {enrollError && (
          <div style={{
            fontSize: 11.5, color: "var(--lm-red)", padding: "7px 10px", borderRadius: 7,
            background: "var(--lm-red-dim)", border: "1px solid var(--lm-red-glow)",
          }}>{enrollError}</div>
        )}
        <button
          onClick={onSubmit}
          disabled={enrolling || !enrollFile || !enrollName.trim()}
          className={"lm-u-btn" + (enrolling || !enrollFile || !enrollName.trim() ? "" : " lm-u-btn-primary")}
          style={{
            ...btnStyle,
            width: "100%", padding: "10px 14px", fontSize: 12.5, borderRadius: 8,
            display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 7,
          }}
        >
          {enrolling
            ? <><Loader2 size={14} className="lm-spin" /> Adding…</>
            : <><UserPlus size={14} /> Add User</>}
        </button>
      </div>
      </div>
    </div>,
    document.body,
  );
}
