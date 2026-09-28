import { useRef, useState } from "react";
import { MicVocal, Mic, Loader2 } from "lucide-react";
import { C, Field, SectionCard, LABEL_STYLE } from "@/components/setup/shared";
import { pickVoicePhrases, pickVoiceIntro, VOICE_DURATION_SEC } from "@/components/setup/voice-phrases";
import type { FaceOwner } from "@/hooks/setup/useFaceEnroll";
import { hwUrl } from "@/lib/api";

type EnrollReply = { status?: string; detail?: string; message?: string };

// Voice enroll — remote-trigger the device's /speaker/record-enroll. The device captures
// via its own mic; web only does countdown UI. Sharing label with face enroll
// keeps both biometrics in one per-user folder. State stays local since
// nothing outside this section reads it.
export function VoiceSection({
  active, sttLanguage, faceOwners, loadFaceOwners,
}: {
  active: boolean;
  sttLanguage: string;
  faceOwners: FaceOwner[];
  loadFaceOwners: () => Promise<void>;
}) {
  const VOICE_PHRASES = pickVoicePhrases(sttLanguage);
  const VOICE_INTRO = pickVoiceIntro(sttLanguage);

  const [voiceLabel, setVoiceLabel] = useState("");
  const [voicePhase, setVoicePhase] = useState<"idle" | "countdown" | "recording" | "processing">("idle");
  const [voiceCountdown, setVoiceCountdown] = useState(0);
  const [voiceMsg, setVoiceMsg] = useState<string | null>(null);
  const voiceTickRef = useRef<number | null>(null);
  const [voiceExpanded, setVoiceExpanded] = useState<Record<string, boolean>>({});
  const toggleVoiceExpanded = (label: string) =>
    setVoiceExpanded((prev) => ({ ...prev, [label]: !prev[label] }));

  const removeVoiceFile = async (name: string, file: string) => {
    if (!confirm(`Delete voice sample "${file}" for "${name}"?`)) return;
    try {
      await fetch("/api/voice/file/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, file }),
      });
      loadFaceOwners();
    } catch { /* ignore */ }
  };

  const startVoiceEnroll = () => {
    if (!voiceLabel.trim()) {
      setVoiceMsg("Enter a name first");
      return;
    }
    setVoiceMsg(null);
    setVoicePhase("countdown");
    let pre = 3;
    setVoiceCountdown(pre);
    voiceTickRef.current = window.setInterval(() => {
      pre -= 1;
      if (pre > 0) {
        setVoiceCountdown(pre);
        return;
      }
      if (voiceTickRef.current) clearInterval(voiceTickRef.current);
      setVoicePhase("recording");
      let remaining = VOICE_DURATION_SEC;
      setVoiceCountdown(remaining);
      voiceTickRef.current = window.setInterval(() => {
        remaining -= 1;
        if (remaining <= 0) {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("processing");
          setVoiceCountdown(0);
        } else {
          setVoiceCountdown(remaining);
        }
      }, 1000);
      fetch(hwUrl("/speaker/record-enroll"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: voiceLabel.trim().toLowerCase(), duration_sec: VOICE_DURATION_SEC }),
      })
        // A crash upstream answers in plain text ("Internal Server Error"), so
        // parsing blind reported a JSON syntax error instead of the real cause.
        .then(async (r) => {
          const body = await r.text();
          try {
            return { ok: r.ok, data: JSON.parse(body) as EnrollReply };
          } catch {
            return { ok: false, data: { message: body.trim().slice(0, 200) || `HTTP ${r.status}` } };
          }
        })
        .then(({ ok, data }) => {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("idle");
          setVoiceCountdown(0);
          if (ok && data.status === "ok") {
            setVoiceMsg(`Enrolled "${voiceLabel.trim().toLowerCase()}"`);
            loadFaceOwners();
          } else {
            setVoiceMsg(`Error: ${data.detail ?? data.message ?? "enroll failed"}`);
          }
        })
        .catch((e) => {
          if (voiceTickRef.current) clearInterval(voiceTickRef.current);
          setVoicePhase("idle");
          setVoiceCountdown(0);
          setVoiceMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
        });
    }, 1000);
  };

  const withVoice = faceOwners.filter((p) => (p.voice_samples?.length ?? 0) > 0);

  return (
    <SectionCard
      id="voice"
      title="My Voice (optional)"
      active={active}
      description={VOICE_INTRO}
      icon={<MicVocal size={17} />}
    >
      <Field label="Name" id="voice_label" value={voiceLabel} onChange={setVoiceLabel} placeholder="e.g. Leo" />
      <div style={{ ...LABEL_STYLE, marginBottom: 8 }}>Read these aloud</div>
      <div style={{
        background: C.surface, border: `1px solid ${C.border}`, borderRadius: 12,
        padding: "6px 8px", marginBottom: 14,
      }}>
        {VOICE_PHRASES.map((p, i) => (
          <div key={i} style={{
            display: "flex", alignItems: "flex-start", gap: 10,
            padding: "9px 8px",
            borderBottom: i < VOICE_PHRASES.length - 1 ? `1px solid ${C.border}` : "none",
          }}>
            <span style={{
              flexShrink: 0, width: 20, height: 20, borderRadius: 999,
              background: C.amberDim, color: C.amber,
              fontSize: 11, fontWeight: 700,
              display: "flex", alignItems: "center", justifyContent: "center",
              marginTop: 1,
            }}>{i + 1}</span>
            <span style={{ fontSize: 13, lineHeight: 1.5, color: C.text }}>{p}</span>
          </div>
        ))}
      </div>
      {voiceMsg && (
        <div style={{
          fontSize: 11, padding: "6px 10px", borderRadius: 8, marginBottom: 10,
          background: voiceMsg.startsWith("Error") ? "var(--lm-red-dim)" : "var(--lm-green-dim)",
          color: voiceMsg.startsWith("Error") ? C.red : C.green,
        }}>{voiceMsg}</div>
      )}
      <button
        type="button"
        onClick={startVoiceEnroll}
        disabled={!voiceLabel.trim() || voicePhase !== "idle"}
        style={{
          width: "100%", padding: "11px 0", borderRadius: 10, fontSize: 13, fontWeight: 600,
          cursor: voicePhase === "idle" && voiceLabel.trim() ? "pointer" : "not-allowed",
          transition: "all 0.15s",
          display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          background: voicePhase === "recording" ? "var(--lm-red-dim)"
            : voicePhase === "countdown" ? "var(--lm-amber-dim)"
            : voicePhase === "processing" ? C.surface
            : !voiceLabel.trim() ? C.surface : "var(--lm-green-dim)",
          border: `1px solid ${voicePhase === "recording" ? "var(--lm-red-glow)"
            : voicePhase === "countdown" ? C.amber
            : !voiceLabel.trim() ? C.border : "var(--lm-green-glow)"}`,
          color: voicePhase === "recording" ? C.red
            : voicePhase === "countdown" ? C.amber
            : voicePhase === "processing" ? C.textDim
            : !voiceLabel.trim() ? C.textMuted : C.green,
        }}
      >
        {voicePhase === "idle" && <><Mic size={15} />{`Start Recording (${VOICE_DURATION_SEC}s on robot)`}</>}
        {voicePhase === "countdown" && `Get ready... ${voiceCountdown}`}
        {voicePhase === "recording" && (
          <>
            <span style={{
              width: 9, height: 9, borderRadius: 999, background: C.red,
              boxShadow: "0 0 6px var(--lm-red-glow)", animation: "lm-pulse-dot 1s ease-in-out infinite",
            }} />
            {`Recording on robot — read aloud (${voiceCountdown}s)`}
          </>
        )}
        {voicePhase === "processing" && <><Loader2 size={15} className="lm-spin-ico" />Processing…</>}
      </button>
      {withVoice.length > 0 && (
        <div style={{ marginTop: 16, borderTop: `1px solid ${C.border}`, paddingTop: 14 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.textDim, textTransform: "uppercase", letterSpacing: "0.09em", marginBottom: 10 }}>
            Voice Files
          </div>
          {withVoice.map((p) => {
            const expanded = !!voiceExpanded[p.label];
            return (
              <div key={p.label} style={{ padding: "10px 0", borderBottom: `1px solid ${C.border}` }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: expanded ? 8 : 0 }}>
                  <button
                    type="button"
                    onClick={() => toggleVoiceExpanded(p.label)}
                    style={{
                      flex: 1, display: "flex", alignItems: "center", gap: 8,
                      background: "none", border: "none", cursor: "pointer", padding: 0,
                      textAlign: "left", color: C.text,
                    }}
                  >
                    <span style={{ fontSize: 11, color: C.textMuted, transition: "transform 0.15s", transform: expanded ? "rotate(90deg)" : "none" }}>▶</span>
                    <span style={{ fontSize: 13, fontWeight: 600 }}>{p.label}</span>
                    <span style={{ fontSize: 10, color: C.textMuted, fontWeight: 400 }}>({p.voice_samples!.length} file{p.voice_samples!.length !== 1 ? "s" : ""})</span>
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      if (!confirm(`Remove ALL voice files for "${p.label}"? Face data is preserved.`)) return;
                      try {
                        await fetch(hwUrl("/speaker/remove"), {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ name: p.label }),
                        });
                        loadFaceOwners();
                      } catch { /* ignore */ }
                    }}
                    style={{
                      background: "none", border: `1px solid ${C.border}`, borderRadius: 5,
                      cursor: "pointer", fontSize: 10, color: C.red, padding: "3px 8px",
                    }}
                  >
                    Remove all
                  </button>
                </div>
                {expanded && p.voice_samples!.map((file) => {
                  const ext = file.toLowerCase().split(".").pop() || "";
                  const url = hwUrl(`/face/file/${p.label}/voice/${encodeURIComponent(file)}`);
                  const isAudio = ["wav", "ogg", "mp3", "webm", "m4a"].includes(ext);
                  const viewLabel = ["json", "jsonl", "txt"].includes(ext) ? "view" : "open";
                  return (
                    <div key={file} title={file} style={{
                      display: "flex", alignItems: "center", gap: 6, padding: "3px 0",
                      fontSize: 11, color: C.textDim,
                    }}>
                      <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontFamily: "monospace" }}>
                        {file}
                      </span>
                      {isAudio ? (
                        <>
                          <audio controls src={url} style={{ width: 180, height: 24 }} />
                          <button type="button" onClick={() => removeVoiceFile(p.label, file)}
                            style={{ background: "none", border: "none", cursor: "pointer", color: C.red, fontSize: 14, lineHeight: 1, padding: "0 4px" }} title="Delete">
                            ×
                          </button>
                        </>
                      ) : (
                        <a href={url} target="_blank" rel="noopener noreferrer"
                          style={{ fontSize: 10, color: C.amber, textDecoration: "none", padding: "2px 6px", border: `1px solid ${C.border}`, borderRadius: 4 }}>
                          {viewLabel}
                        </a>
                      )}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      )}
    </SectionCard>
  );
}
