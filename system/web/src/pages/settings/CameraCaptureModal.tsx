import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Camera, X, SwitchCamera, Check } from "lucide-react";
import { C } from "@/components/setup/shared";
import { useTheme } from "@/lib/useTheme";

// Live-preview webcam capture for face enroll on desktop (and any device with a usable getUserMedia stream).
export function CameraCaptureModal({
  onCapture, onClose,
}: {
  onCapture: (files: File[]) => void;
  onClose: () => void;
}) {
  const [, , themeClass] = useTheme();
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [facing, setFacing] = useState<"user" | "environment">("user");
  const [shots, setShots] = useState<{ url: string; file: File }[]>([]);

  const stopStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  const startStream = useCallback(async (mode: "user" | "environment") => {
    setError(null);
    setReady(false);
    stopStream();
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("Camera is not available in this browser, or the page is not served over HTTPS.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: mode, width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play().catch(() => { /* autoplay race; play resumes on metadata */ });
      }
      setReady(true);
    } catch (e) {
      const name = e instanceof DOMException ? e.name : "";
      if (name === "NotAllowedError") setError("Camera permission was denied. Allow camera access and try again.");
      else if (name === "NotFoundError") setError("No camera was found on this device.");
      else setError(e instanceof Error ? e.message : "Could not open the camera.");
    }
  }, [stopStream]);

  useEffect(() => {
    startStream(facing);
    return () => stopStream();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [facing]);

  useEffect(() => {
    return () => { shots.forEach((s) => URL.revokeObjectURL(s.url)); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const snap = useCallback(() => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const file = new File([blob], `capture-${shots.length + 1}.jpg`, { type: "image/jpeg" });
      setShots((prev) => [...prev, { url: URL.createObjectURL(blob), file }]);
    }, "image/jpeg", 0.92);
  }, [shots.length]);

  const removeShot = (idx: number) => {
    setShots((prev) => {
      const target = prev[idx];
      if (target) URL.revokeObjectURL(target.url);
      return prev.filter((_, i) => i !== idx);
    });
  };

  const commit = () => {
    if (shots.length === 0) return;
    onCapture(shots.map((s) => s.file));
    onClose();
  };

  // Portal to <body>: an ancestor transform would become the containing block.
  return createPortal(
    <div
      className={`lm-root ${themeClass}`}
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)",
        display: "flex", justifyContent: "center", alignItems: "center",
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Take photos"
        className="lm-pop"
        style={{
          width: "min(560px, 100%)", maxHeight: "90vh", minHeight: "52dvh",
          padding: "20px 22px",
          background: "var(--lm-card)",
          border: `1px solid ${C.border}`,
          borderRadius: 14,
          boxShadow: "0 1px 2px rgba(0,0,0,0.18), 0 8px 24px -16px rgba(0,0,0,0.5)",
          display: "flex", flexDirection: "column", gap: 12,
        }}
      >
        <div style={{ flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Camera size={16} style={{ color: C.amber }} />
            <span style={{ fontSize: 14.5, fontWeight: 600, color: C.text }}>Take photos</span>
          </div>
          <button
            type="button" onClick={onClose} aria-label="Close"
            style={{
              width: 30, height: 30, borderRadius: 8, background: C.surface,
              border: `1px solid ${C.border}`, color: C.textDim, cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <X size={15} />
          </button>
        </div>

        <div style={{
          flex: 1, minHeight: 200, position: "relative", width: "100%",
          borderRadius: 12, overflow: "hidden",
          background: "#000", border: `1px solid ${C.border}`,
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          {error ? (
            <div style={{ fontSize: 12.5, color: C.textDim, textAlign: "center", padding: 20, lineHeight: 1.5 }}>
              {error}
            </div>
          ) : (
            <video
              ref={videoRef}
              playsInline
              muted
              style={{
                width: "100%", height: "100%", objectFit: "cover",
                transform: facing === "user" ? "scaleX(-1)" : "none",
              }}
            />
          )}
        </div>

        <div style={{ flexShrink: 0, display: "flex", alignItems: "center", gap: 10 }}>
          <button
            type="button"
            onClick={() => setFacing((f) => (f === "user" ? "environment" : "user"))}
            disabled={!!error}
            title="Switch camera"
            aria-label="Switch camera"
            style={{
              width: 40, height: 40, borderRadius: 10, flexShrink: 0,
              background: C.surface, border: `1px solid ${C.border}`,
              color: error ? C.textMuted : C.textDim, cursor: error ? "not-allowed" : "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <SwitchCamera size={17} />
          </button>
          <button
            type="button"
            onClick={snap}
            disabled={!ready || !!error}
            style={{
              flex: 1, height: 40, borderRadius: 10, fontSize: 12.5, fontWeight: 600,
              cursor: ready && !error ? "pointer" : "not-allowed",
              background: ready && !error ? "var(--lm-amber-dim)" : C.surface,
              border: `1px solid ${ready && !error ? "var(--lm-amber-glow)" : C.border}`,
              color: ready && !error ? C.amber : C.textMuted,
              display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
            }}
          >
            <Camera size={16} />Capture{shots.length > 0 ? ` (${shots.length})` : ""}
          </button>
        </div>

        {shots.length > 0 && (
          <div style={{ flexShrink: 0, display: "flex", gap: 8, overflowX: "auto", paddingTop: 6 }}>
            {shots.map((s, idx) => (
              <div key={s.url} style={{ position: "relative", width: 56, height: 56, flexShrink: 0 }}>
                <img
                  src={s.url}
                  alt={`Capture ${idx + 1}`}
                  style={{
                    width: 56, height: 56, borderRadius: 8, objectFit: "cover",
                    border: `1px solid ${C.border}`, display: "block",
                  }}
                />
                <button
                  type="button"
                  onClick={() => removeShot(idx)}
                  aria-label={`Discard capture ${idx + 1}`}
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
              </div>
            ))}
          </div>
        )}

        <div style={{ flexShrink: 0, display: "flex", gap: 8, justifyContent: "flex-end", marginTop: 4 }}>
          <button
            type="button" onClick={onClose}
            style={{
              padding: "8px 14px", borderRadius: 9, fontSize: 12.5, fontWeight: 500,
              cursor: "pointer", background: C.surface,
              border: `1px solid ${C.border}`, color: C.textDim,
            }}
          >
            Cancel
          </button>
          <button
            type="button" onClick={commit} disabled={shots.length === 0}
            style={{
              padding: "8px 14px", borderRadius: 9, fontSize: 12.5, fontWeight: 600,
              cursor: shots.length > 0 ? "pointer" : "not-allowed",
              background: shots.length > 0 ? "var(--lm-green-dim)" : C.surface,
              border: `1px solid ${shots.length > 0 ? "var(--lm-green-glow)" : C.border}`,
              color: shots.length > 0 ? C.green : C.textMuted,
              display: "flex", alignItems: "center", gap: 7,
            }}
          >
            <Check size={15} />Use {shots.length || ""} photo{shots.length !== 1 ? "s" : ""}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
