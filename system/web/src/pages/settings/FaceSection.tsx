import { useEffect, useRef, useState } from "react";
import { UserCircle, UserPlus, ImagePlus, Camera, Loader2, X } from "lucide-react";
import { hwUrl, fileToBase64 } from "@/lib/api";
import { C, Field, SectionCard, ConfirmDialog, LABEL_STYLE } from "@/components/setup/shared";
import { CameraCaptureModal } from "./CameraCaptureModal";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";

// Phones get the OS camera app via <input capture>
function prefersNativeCapture(): boolean {
  if (typeof window === "undefined") return false;
  const coarse = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  const touch = "ontouchstart" in window || navigator.maxTouchPoints > 0;
  return coarse && touch;
}

interface PendingPhoto {
  file: File;
  url: string;
}

type PendingConfirm =
  | { kind: "owner"; label: string }
  | { kind: "photo"; label: string; filename: string };

// Face enroll for edit-mode owners.
export function FaceSection({
  active, faceOwners, loadFaceOwners,
}: {
  active: boolean;
  faceOwners: FaceOwner[];
  loadFaceOwners: () => Promise<void>;
}) {
  const [faceName, setFaceName] = useState("");
  const [pending, setPending] = useState<PendingPhoto[]>([]);
  const [faceUploading, setFaceUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [faceMsg, setFaceMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [dragging, setDragging] = useState(false);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [nativeCapture] = useState(prefersNativeCapture);
  const faceInputRef = useRef<HTMLInputElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);
  const [faceExpanded, setFaceExpanded] = useState<Record<string, boolean>>({});
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null);
  const toggleFaceExpanded = (label: string) =>
    setFaceExpanded((prev) => ({ ...prev, [label]: !prev[label] }));

  useEffect(() => {
    return () => { pending.forEach((p) => URL.revokeObjectURL(p.url)); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Append picked/dropped image files to the pending list (skips non-images), minting a preview URL for each.
  const addFiles = (files: FileList | File[]) => {
    const imgs = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (imgs.length === 0) return;
    setPending((prev) => [...prev, ...imgs.map((file) => ({ file, url: URL.createObjectURL(file) }))]);
  };

  // "Take photo": native camera app on phones, live-preview modal on desktop.
  const openCamera = () => {
    if (nativeCapture) cameraInputRef.current?.click();
    else setCameraOpen(true);
  };

  const removePending = (idx: number) => {
    setPending((prev) => {
      const target = prev[idx];
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((_, i) => i !== idx);
    });
  };

  const clearPending = () => {
    pending.forEach((p) => URL.revokeObjectURL(p.url));
    setPending([]);
    if (faceInputRef.current) faceInputRef.current.value = "";
  };

  const removeFaceOwner = async (label: string) => {
    try {
      await fetch(hwUrl("/face/remove"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  };

  const removeFacePhoto = async (label: string, filename: string) => {
    try {
      await fetch(hwUrl("/face/photo/remove"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label, filename }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  };

  const handleConfirm = () => {
    if (!confirm) return;
    if (confirm.kind === "owner") removeFaceOwner(confirm.label);
    else removeFacePhoto(confirm.label, confirm.filename);
    setConfirm(null);
  };

  const handleFaceEnroll = async () => {
    if (!faceName.trim() || pending.length === 0) return;
    setFaceUploading(true);
    setFaceMsg(null);
    setUploadProgress(0);
    const label = faceName.trim().toLowerCase();
    let ok = 0;
    let lastErr = "";
    for (const { file } of pending) {
      try {
        const b64 = await fileToBase64(file);
        const resp = await fetch(hwUrl("/face/enroll"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ label, image_base64: b64 }),
        });
        // Parse defensively: proxy errors return HTML.
        const raw = await resp.text();
        let data: { detail?: string; message?: string } = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch { /* non-JSON (HTML error page) */ }
        if (resp.ok) {
          ok++;
        } else {
          lastErr = data.detail || data.message || "Couldn't reach the camera. Make sure the robot is on and connected.";
        }
      } catch {
        lastErr = "Couldn't reach the camera. Make sure the robot is on and connected.";
      }
      setUploadProgress((n) => (n ?? 0) + 1);
    }
    if (ok > 0) {
      setFaceMsg({
        type: lastErr ? "error" : "success",
        text: lastErr
          ? `Saved ${ok} of ${pending.length} photos — some didn't upload. Please try again.`
          : `Saved ${ok} photo${ok !== 1 ? "s" : ""} for ${faceName.trim()}.`,
      });
      setFaceName("");
      clearPending();
      loadFaceOwners();
    } else {
      setFaceMsg({ type: "error", text: lastErr });
    }
    setUploadProgress(null);
    setFaceUploading(false);
  };

  const enrolled = faceOwners.filter((p) => p.photo_count > 0);
  const canEnroll = !!faceName.trim() && pending.length > 0;

  return (
    <SectionCard
      id="face"
      title="Face Enroll (optional)"
      active={active}
      description="Upload photos of the owner so your robot can recognize them."
      icon={<UserCircle size={17} />}
    >
      <Field label="Name" id="face_name" value={faceName} onChange={setFaceName} placeholder="e.g. Leo" />
      <div style={{ marginBottom: 14 }}>
        <label style={{ ...LABEL_STYLE, marginBottom: 8 }}>Photos</label>
        <input
          ref={faceInputRef}
          type="file"
          accept="image/*"
          multiple
          // Reset so re-picking the same file fires onChange.
          onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }}
          style={{ display: "none" }}
        />
        <input
          ref={cameraInputRef}
          type="file"
          accept="image/*"
          capture="user"
          onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }}
          style={{ display: "none" }}
        />
        <button
          type="button"
          onClick={() => faceInputRef.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={(e) => { e.preventDefault(); setDragging(false); }}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
          }}
          className="lm-face-drop"
          style={{
            width: "100%", boxSizing: "border-box", cursor: "pointer",
            display: "flex", alignItems: "center", gap: 12,
            padding: "14px 16px", borderRadius: 12,
            border: `1px dashed ${dragging || pending.length ? "var(--lm-amber-glow)" : C.border}`,
            background: dragging || pending.length ? "var(--lm-amber-dim)" : C.surface,
            color: dragging || pending.length ? C.amber : C.textDim,
          }}
        >
          <ImagePlus size={20} style={{ flexShrink: 0 }} />
          <span style={{ flex: 1, textAlign: "left", fontSize: 13, fontWeight: pending.length ? 600 : 400 }}>
            {dragging
              ? "Drop photos to add"
              : pending.length
                ? `${pending.length} photo${pending.length !== 1 ? "s" : ""} selected`
                : "Choose photos or drag them here…"}
          </span>
          <span style={{ flexShrink: 0, fontSize: 11, color: C.textMuted }}>
            {pending.length ? "Add more" : "Browse"}
          </span>
        </button>

        <button
          type="button"
          onClick={openCamera}
          className="lm-face-cam"
          style={{
            width: "100%", boxSizing: "border-box", cursor: "pointer", marginTop: 8,
            display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
            padding: "10px 16px", borderRadius: 10,
            border: `1px solid ${C.border}`, background: C.surface, color: C.textDim,
            fontSize: 12.5, fontWeight: 500,
          }}
        >
          <Camera size={16} />Take photo
        </button>

        {pending.length > 0 && (
          <div style={{ marginTop: 10 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={{ fontSize: 11, color: C.textMuted }}>
                {pending.length} ready to enroll
              </span>
              <button
                type="button"
                onClick={clearPending}
                style={{
                  background: "none", border: "none", cursor: "pointer",
                  fontSize: 11, color: C.textMuted, padding: 0,
                }}
              >
                Clear all
              </button>
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {pending.map((p, idx) => (
                <div key={p.url} style={{ position: "relative", width: 64, height: 64 }}>
                  <img
                    src={p.url}
                    alt={p.file.name}
                    title={p.file.name}
                    style={{
                      width: 64, height: 64, borderRadius: 8, objectFit: "cover",
                      border: `1px solid ${C.border}`, display: "block",
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => removePending(idx)}
                    title={`Remove ${p.file.name}`}
                    aria-label={`Remove ${p.file.name}`}
                    style={{
                      position: "absolute", top: -6, right: -6,
                      width: 20, height: 20, borderRadius: "50%",
                      background: C.bg, border: `1px solid ${C.border}`,
                      cursor: "pointer", color: C.red, padding: 0,
                      display: "flex", alignItems: "center", justifyContent: "center",
                    }}
                  >
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
      {faceMsg && (
        <div
          role="status"
          aria-live="polite"
          style={{
            fontSize: 11, padding: "6px 10px", borderRadius: 8, marginBottom: 10,
            background: faceMsg.type === "error" ? "var(--lm-red-dim)" : "var(--lm-green-dim)",
            color: faceMsg.type === "error" ? C.red : C.green,
          }}
        >{faceMsg.text}</div>
      )}
      {pending.length > 0 && !faceName.trim() && !faceUploading && (
        <div style={{ fontSize: 11, color: C.textMuted, marginBottom: 8, textAlign: "center" }}>
          Enter a name above to enroll these {pending.length} photo{pending.length !== 1 ? "s" : ""}.
        </div>
      )}
      <button
        type="button"
        onClick={handleFaceEnroll}
        disabled={!canEnroll || faceUploading}
        style={{
          width: "100%", padding: "10px 0", borderRadius: 10, fontSize: 12.5,
          fontWeight: 600, cursor: faceUploading ? "wait" : canEnroll ? "pointer" : "not-allowed",
          transition: "all 0.15s",
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          background: !canEnroll ? C.surface : "var(--lm-green-dim)",
          border: `1px solid ${!canEnroll ? C.border : "var(--lm-green-glow)"}`,
          color: !canEnroll ? C.textMuted : C.green,
        }}
      >
        {faceUploading
          ? <><Loader2 size={15} className="lm-spin-ico" />Uploading {uploadProgress ?? 0}/{pending.length}…</>
          : <><UserPlus size={15} />Enroll Face</>}
      </button>
      {enrolled.length > 0 && (
        <div style={{ marginTop: 16, borderTop: `1px solid ${C.border}`, paddingTop: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.09em", marginBottom: 10 }}>
            Face Photos
          </div>
          {enrolled.map((p) => {
            const expanded = !!faceExpanded[p.label];
            return (
              <div key={p.label} style={{ padding: "10px 0", borderBottom: `1px solid ${C.border}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: expanded ? 8 : 0 }}>
                  <button
                    type="button"
                    onClick={() => toggleFaceExpanded(p.label)}
                    aria-expanded={expanded}
                    style={{
                      // minWidth:0 lets the label truncate.
                      flex: 1, minWidth: 0, display: "flex", alignItems: "center", gap: 8,
                      background: "none", border: "none", cursor: "pointer", padding: 0,
                      textAlign: "left", color: C.text,
                    }}
                  >
                    <span style={{ flexShrink: 0, fontSize: 11, color: C.textMuted, transition: "transform 0.15s", transform: expanded ? "rotate(90deg)" : "none" }}>▶</span>
                    <span style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.label}</span>
                    <span style={{ flexShrink: 0, fontSize: 10, color: C.textMuted, fontWeight: 400 }}>({p.photo_count} photo{p.photo_count !== 1 ? "s" : ""})</span>
                  </button>
                  {p.label !== "unknown" && (
                    <button
                      type="button"
                      onClick={() => setConfirm({ kind: "owner", label: p.label })}
                      style={{
                        flexShrink: 0,
                        background: "none", border: `1px solid ${C.border}`, borderRadius: 5,
                        cursor: "pointer", fontSize: 10, color: C.red, padding: "3px 8px",
                      }}
                    >
                      Remove all
                    </button>
                  )}
                </div>
                {expanded && p.photos.length > 0 && (
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    {p.photos.map((photo) => (
                      <div key={photo} style={{ position: "relative", width: 56, height: 56 }}>
                        <img
                          src={hwUrl(`/face/photo/${encodeURIComponent(p.label)}/${encodeURIComponent(photo)}`)}
                          title={photo}
                          onClick={() => window.open(hwUrl(`/face/photo/${encodeURIComponent(p.label)}/${encodeURIComponent(photo)}`), "_blank", "noopener,noreferrer")}
                          style={{
                            width: 56, height: 56, borderRadius: 8, objectFit: "cover",
                            border: `1px solid ${C.border}`, cursor: "pointer", display: "block",
                          }}
                        />
                        {p.label !== "unknown" && (
                          <button
                            type="button"
                            onClick={(e) => { e.stopPropagation(); setConfirm({ kind: "photo", label: p.label, filename: photo }); }}
                            title={`Delete ${photo}`}
                            aria-label={`Delete ${photo}`}
                            style={{
                              position: "absolute", top: -6, right: -6,
                              width: 18, height: 18, borderRadius: "50%",
                              background: C.bg, border: `1px solid ${C.border}`,
                              cursor: "pointer", color: C.red, padding: 0,
                              display: "flex", alignItems: "center", justifyContent: "center",
                            }}
                          >
                            <X size={11} />
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      {confirm && (
        <ConfirmDialog
          danger
          title={confirm.kind === "owner" ? `Remove "${confirm.label}"?` : "Delete photo?"}
          message={confirm.kind === "owner"
            ? "All enrolled photos for this owner will be removed."
            : `Delete "${confirm.filename}" for "${confirm.label}"?`}
          confirmLabel={confirm.kind === "owner" ? "Remove all" : "Delete"}
          onConfirm={handleConfirm}
          onCancel={() => setConfirm(null)}
        />
      )}
      {cameraOpen && (
        <CameraCaptureModal
          onCapture={addFiles}
          onClose={() => setCameraOpen(false)}
        />
      )}
    </SectionCard>
  );
}
