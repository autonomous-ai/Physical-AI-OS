import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Satellite, Globe, Eye, Volume2, Cpu, Drama, Clapperboard, Bot, Tag, Wifi, LayoutDashboard, Power } from "lucide-react";
import { S } from "./styles";
import { API, HW } from "./types";

import "./robot-status.css";

const EMOTION_EMOJI: Record<string, string> = {
  happy: "😊", curious: "🤔", thinking: "💭", sad: "😢", excited: "🤩",
  shy: "😳", shock: "😱", idle: "😐", listening: "👂", laugh: "😄",
  confused: "😕", sleepy: "😴", greeting: "👋", goodbye: "👋", acknowledge: "👍",
  stretching: "🙆", caring: "🤗", music_chill: "🎵", music_strong: "🎸",
  scan: "👀", nod: "👍", headshake: "🙅",
};

type OtaVersions = Record<string, { current?: string; target?: string; update_available?: boolean }>;

export type OverviewCache = {
  versions?: OtaVersions;
  presets?: { emotions: string[]; colors: Record<string, string> };
};

async function fetchOtaVersions(): Promise<OtaVersions | null> {
  try {
    const response = await fetch(`${API}/system/ota-versions`);
    if (!response.ok) return null;
    const body = await response.json();
    return body?.data as OtaVersions | null;
  } catch {
    return null;
  }
}

function rgbToHex(rgb: number[]): string {
  return "#" + rgb.map(c => c.toString(16).padStart(2, "0")).join("");
}

function useEmotionPresets(cache: OverviewCache) {
  const [emotions, setEmotions] = useState<string[]>(() => cache.presets?.emotions ?? []);
  const [colors, setColors] = useState<Record<string, string>>(() => cache.presets?.colors ?? {});
  useEffect(() => {
    let cancelled = false;
    fetch(`${HW}/emotion/presets`)
      .then(r => { if (!r.ok) throw new Error("Emotion presets unavailable"); return r.json(); })
      .then((data: Record<string, { color: number[]; effect: string; speed: number }>) => {
        if (cancelled) return;
        const names = Object.keys(data);
        const c: Record<string, string> = {};
        for (const [name, preset] of Object.entries(data)) {
          c[name] = rgbToHex(preset.color);
        }
        cache.presets = { emotions: names, colors: c };
        setEmotions(names);
        setColors(c);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [cache]);
  return { emotions, colors };
}
import type { SystemInfo, NetworkInfo, HWHealth, OCStatus, PresenceInfo, VoiceStatus, ServoState, DisplayState, AudioVolume, LEDColor, SceneInfo } from "./types";
import { StatusDot, HWBadge, SignalBars, Skeleton, SkeletonRows, SoftwareUpdateButton, StatRow, StatusBadge, STATUS_TONE, CardLabel, RestartAgentButton, DevicePowerButtons } from "./components";
import { RestartServiceButton } from "./RestartServiceButton";
import { formatUptime, formatAgo, useCountUp } from "./utils";

export function OverviewSection({
  cache,
  sys,
  net,
  hw,
  oc,
  presence,
  voice,
  servo,
  displayState,
  audio,
  musicPlaying,
  speakerMuted,
  ledColor,
  sceneInfo,
  hasEmotion,
  hasMotion,
  webVersion,
  halVersion,
  onSceneActivate,
  onMicMutedChange,
  onSpeakerMutedChange,
  onTTSStop,
  onPlaybackLive,
  onEmotionPick,
  onServoPlay,
  onServoRelease,
}: {
  cache: OverviewCache;
  sys: SystemInfo | null;
  net: NetworkInfo | null;
  hw: HWHealth | null;
  oc: OCStatus | null;
  presence: PresenceInfo | null;
  voice: VoiceStatus | null;
  servo: ServoState | null;
  displayState: DisplayState | null;
  audio: AudioVolume | null;
  musicPlaying: boolean;
  speakerMuted: boolean;
  ledColor: LEDColor | null;
  sceneInfo: SceneInfo | null;
  hasEmotion: boolean;
  hasMotion: boolean;
  webVersion: string;
  halVersion: string | null;
  onSceneActivate: (scene: string) => void;
  onMicMutedChange: (muted: boolean) => void;
  onSpeakerMutedChange: (muted: boolean) => void;
  onTTSStop: () => void;
  onPlaybackLive?: (tts: boolean, music: boolean) => void;
  onEmotionPick: (emotion: string) => void;
  onServoPlay: (recording: string) => void;
  onServoRelease: () => void;
}) {
  const { emotions: ALL_EMOTIONS, colors: EMOTION_COLOR } = useEmotionPresets(cache);
  const emotion = oc?.emotion ?? "";
  const emotionColor = EMOTION_COLOR[emotion] ?? "var(--lm-text-muted)";
  const emotionEmoji = EMOTION_EMOJI[emotion] ?? "✦";

  // OTA update buttons are debug-only.
  const isDebug = new URLSearchParams(window.location.search).get("debug") === "true";

  const [otaVersions, setOtaVersions] = useState<OtaVersions>(() => cache.versions ?? {});
  const refreshOtaVersions = useCallback(() => {
    void fetchOtaVersions().then((versions) => {
      if (versions) { cache.versions = versions; setOtaVersions(versions); }
    });
  }, [cache]);
  useEffect(() => {
    let cancelled = false;
    fetchOtaVersions().then((versions) => {
      if (!cancelled && versions) {
        cache.versions = versions;
        setOtaVersions(versions);
      }
    });
    return () => { cancelled = true; };
  }, [cache]);
  // held_by_floor is deliberately ignored: this installs on this one device.
  const canUpdate = (target: string) => isDebug && !!otaVersions[target]?.update_available;

  const [otaUpdating, setOtaUpdating] = useState<string[]>([]);
  const otaUpdatingRef = useRef<string[]>([]);
  const [justTriggered, setJustTriggered] = useState<Record<string, number>>({});
  const pokePollRef = useRef<() => void>(() => {});
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const r = await fetch(`${API}/system/ota-updating`);
        const j = r.ok ? await r.json() : null;
        const list: string[] = j?.data?.updating ?? [];
        if (!cancelled) {
          const wasBusy = otaUpdatingRef.current.length > 0;
          otaUpdatingRef.current = list;
          setOtaUpdating(list);
          if (wasBusy && list.length === 0) void refreshOtaVersions();
        }
      } catch { /* bootstrap down → treat as "nothing running" */ }
      if (!cancelled) timer = setTimeout(poll, otaUpdatingRef.current.length > 0 ? 2000 : 10000);
    };
    pokePollRef.current = () => { if (timer) clearTimeout(timer); void poll(); };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [refreshOtaVersions]);

  const onUpdateTriggered = useCallback((target: string) => {
    setJustTriggered((prev) => ({ ...prev, [target]: Date.now() }));
    pokePollRef.current();
    setTimeout(() => {
      setJustTriggered((prev) => {
        const next = { ...prev };
        delete next[target];
        return next;
      });
      void refreshOtaVersions();
    }, 6000);
  }, [refreshOtaVersions]);

  const isUpdating = (target: string) => otaUpdating.includes(target) || target in justTriggered;

  const [localVolume, setLocalVolume] = useState<number | null>(null);
  const draggingVolume = useRef(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    if (!draggingVolume.current && audio?.volume != null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- the slider is an uncontrolled-while-dragging input: server volume may only overwrite it BETWEEN drags. Deriving it during render would yank the handle out from under the operator's finger mid-drag.
      setLocalVolume(audio.volume);
    }
  }, [audio?.volume]);

  const animatedLinkRate = useCountUp(net?.linkRate ?? 0);

  // Present the robot's allowed volume range as 0–100%; HAL still receives raw mixer percentages.
  const volumeCeiling = audio?.max_volume ?? 100;
  const rawVolume = Math.max(0, Math.min(localVolume ?? audio?.volume ?? 0, volumeCeiling));
  const volumeValue = volumeCeiling > 0 ? Math.round((rawVolume / volumeCeiling) * 100) : 0;
  const animatedVolume = useCountUp(volumeValue);

  const commitVolume = useCallback((vol: number) => {
    draggingVolume.current = false;
    setDragging(false);
    fetch(`${HW}/audio/volume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: Math.round((vol / 100) * volumeCeiling) }),
    })
      // Snap to what HAL actually applied.
      .then((r) => r.json())
      .then((r) => {
        if (typeof r?.volume === "number") setLocalVolume(r.volume);
      })
      .catch(() => {});
  }, [volumeCeiling]);

  const monCard = { ...S.card, boxShadow: undefined };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

      <div className="lm-mon-hero">
        <div style={{ position: "relative", zIndex: 1, display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
            <div style={{
              width: 44, height: 44, borderRadius: 12, flexShrink: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
              background: "var(--lm-amber-dim)", color: "var(--lm-amber)",
              boxShadow: "inset 0 0 0 1px var(--lm-amber-glow)",
            }} aria-hidden><LayoutDashboard size={22} /></div>
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 19, fontWeight: 700, color: "var(--lm-text)", letterSpacing: "-0.3px", lineHeight: 1.2 }}>
                Robot Overview
              </div>
              <div style={{ fontSize: 12, color: "var(--lm-text-dim)", marginTop: 2 }}>
                Live status across agent, network, presence & hardware
              </div>
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <HeroChip
              icon={<Bot size={14} />}
              label="Agent"
              value={oc ? (oc.connected ? "Online" : "Offline") : "Loading"}
              tone={oc ? (oc.connected ? "ok" : "error") : "neutral"}
            />
            <HeroChip icon={<Wifi size={14} />} label="IP" value={net?.ip ?? "—"} tone="neutral" />
            <HeroChip
              icon={<Eye size={14} />}
              label="Presence"
              value={presence?.state ? presence.state[0].toUpperCase() + presence.state.slice(1) : "—"}
              tone={presence?.state === "active" ? "active" : "neutral"}
            />
          </div>
        </div>
      </div>

      <div className="lm-grid-4 lm-overview-status-grid">
        <div className="lm-mon-card" style={{ ...monCard, position: "relative" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Satellite size={13} />} text="Agent Gateway" />
            <StatusBadge text={oc ? (oc.connected ? "ONLINE" : "OFFLINE") : "LOADING"} tone={!oc ? "idle" : undefined} ok={!!oc?.connected} pulse={!!oc?.connected} />
          </div>
          {oc ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="Agent" value={oc.name} />
              {oc.version && <StatRow label="Version" value={oc.version} mono />}
              <StatRow label="Session" value={
                <span style={{
                  fontSize: 10, padding: "1px 6px", borderRadius: 4, fontWeight: 600,
                  background: oc.sessionKey ? "rgba(52,211,153,0.1)" : "rgba(80,74,60,0.4)",
                  color: oc.sessionKey ? "var(--lm-green)" : "var(--lm-text-muted)",
                }}>
                  {oc.sessionKey ? "Active" : "Pending"}
                </span>
              } />
              {oc.emotion && <StatRow label="Emotion" value={oc.emotion} color="var(--lm-amber)" />}
            </div>
          ) : <SkeletonRows lines={3} />}
          <RestartAgentButton agentName={oc?.name} />
        </div>

        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Globe size={13} />} text="Network" />
          </div>
          {net ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="SSID" value={net.ssid || "—"} />
              <StatRow label="IP" value={net.ip} color="var(--lm-teal)" />
              {net.tailscaleIp && <StatRow label="Tailscale" value={net.tailscaleIp} color="var(--lm-teal)" />}
              <StatRow label="Internet" value={net.internet ? `Connected${net.pingMs ? ` · ${net.pingMs} ms` : ""}` : "No"} color={net.internet ? "var(--lm-green)" : "var(--lm-red)"} />
              <StatRow label="Speed" value={
                <span style={{ display: "flex", alignItems: "center", gap: 6 }} title={`Signal ${net.signal} dBm`}>
                  <SignalBars value={net.signal} />
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text)" }}>
                    {net.linkRate > 0 ? `${animatedLinkRate} Mbps` : "—"}
                  </span>
                </span>
              } />
              <StatRow label="MAC" value={net.mac || "—"} mono />
            </div>
          ) : <SkeletonRows lines={5} />}
        </div>

        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Eye size={13} />} text="Presence" />
            <StatusBadge text={(presence?.state ?? "—").toUpperCase()} tone={presence?.state === "active" ? "active" : "idle"} pulse={presence?.state === "active"} />
          </div>
          {presence ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <StatRow label="Sensing" value={presence.enabled ? "On" : "Off"} color={presence.enabled ? "var(--lm-green)" : "var(--lm-red)"} />
              <StatRow label="Last motion" value={formatAgo(presence.seconds_since_motion)} />
            </div>
          ) : <SkeletonRows lines={2} />}
        </div>

        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Volume2 size={13} />} text="Audio" /></div>
          {voice ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="lm-audio-row">
                <div className="lm-audio-row-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={voice.voice_available && !voice.mic_muted} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>Mic</span>
                  {voice.mic_muted ? (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(239,68,68,0.12)", color: "#f87171" }}>MUTED</span>
                  ) : voice.voice_listening ? (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "var(--lm-amber-dim)", color: "var(--lm-amber)" }}>LIVE</span>
                  ) : null}
                </div>
                {/* HW mic switch is authoritative: /voice/unmute returns 409 while it is off. */}
                <ToggleButton
                  active={!voice.mic_muted}
                  disabled={voice.hw_mic_switch_muted === true}
                  label={voice.mic_muted ? "Unmute" : "Mute"}
                  onClick={() => onMicMutedChange(!voice.mic_muted)}
                />
              </div>
              {voice.hw_mic_switch_muted === true && (
                <div style={{ fontSize: 10.5, color: "#d97706", marginTop: -6 }}>
                  Hardware mic switch is off — flip the physical switch to unmute.
                </div>
              )}

              <div className="lm-audio-row">
                <div className="lm-audio-row-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={voice.tts_available} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>TTS</span>
                  {voice.tts_speaking && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(167,139,250,0.15)", color: "var(--lm-purple)" }}>SPEAKING</span>
                  )}
                  {musicPlaying && !voice.tts_speaking && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(52,211,153,0.12)", color: "var(--lm-green)" }}>MUSIC</span>
                  )}
                </div>
                {(voice.tts_speaking || musicPlaying) && (
                  <ToggleButton active={false} label="Stop" onClick={onTTSStop} />
                )}
              </div>

              <div className="lm-audio-row">
                <div className="lm-audio-row-label" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <StatusDot ok={!speakerMuted} />
                  <span style={{ fontSize: 13, fontWeight: 600 }}>Speaker</span>
                  {speakerMuted && (
                    <span style={{ fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "rgba(239,68,68,0.12)", color: "#f87171" }}>MUTED</span>
                  )}
                </div>
                <ToggleButton active={!speakerMuted} label={speakerMuted ? "Unmute" : "Mute"}
                  onClick={() => onSpeakerMutedChange(!speakerMuted)} />
              </div>

              <div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Volume</span>
                  <span style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                    <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
                      {dragging ? volumeValue : animatedVolume}%
                    </span>
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={100}
                  aria-label="Volume"
                  disabled={volumeCeiling <= 0}
                  value={volumeValue}
                  onChange={(e) => {
                    draggingVolume.current = true;
                    setDragging(true);
                    setLocalVolume((Number(e.target.value) / 100) * volumeCeiling);
                  }}
                  onMouseUp={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  onTouchEnd={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  onKeyUp={(e) => {
                    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
                      commitVolume(Number(e.currentTarget.value));
                    }
                  }}
                  className="lm-mon-range"
                  style={{
                    width: "100%", cursor: "pointer",
                    ["--lm-fill" as string]: `${volumeValue}%`,
                  }}
                />
              </div>

              <MicLevelBar muted={voice.mic_muted ?? false} onPlayback={onPlaybackLive} />
            </div>
          ) : <AudioSkeleton />}
        </div>
      </div>

      <div className="lm-cluster">
        <div className="lm-cluster-col" style={{ order: 2 }}>
        {hasEmotion && (
        <div style={{
          ...S.card, padding: "14px 16px",
          background: emotion ? `linear-gradient(135deg, var(--lm-bg) 60%, ${emotionColor}18)` : "var(--lm-bg)",
          border: `1px solid ${emotion ? emotionColor + "55" : "var(--lm-border)"}`,
          transition: "all 0.4s ease",
        }}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Drama size={13} />} text="Emotion" /></div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "flex-start", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flex: "0 1 205px", minWidth: 180 }}>
              <div style={{
                fontSize: 36, lineHeight: 1, flexShrink: 0,
                filter: emotion ? `drop-shadow(0 0 8px ${emotionColor}88)` : "none",
                transition: "filter 0.4s ease",
              }}>
                {emotion ? emotionEmoji : "✦"}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 10, color: "var(--lm-text-dim)", marginBottom: 5, textTransform: "uppercase", letterSpacing: "0.08em" }}>
                  Your robot is feeling
                </div>
                <div
                  role="status"
                  aria-live="polite"
                  style={{
                    display: "inline-flex", alignItems: "center", gap: 7, maxWidth: "100%",
                    padding: "4px 8px", borderRadius: 8,
                    background: emotion
                      ? `color-mix(in srgb, ${emotionColor} 14%, var(--lm-surface))`
                      : "var(--lm-surface)",
                    border: `1px solid ${emotion ? `color-mix(in srgb, ${emotionColor} 52%, var(--lm-border))` : "var(--lm-border)"}`,
                    color: emotion ? "var(--lm-text)" : "var(--lm-text-dim)",
                    fontSize: 18, fontWeight: 700, textTransform: "capitalize",
                    transition: "all 0.4s ease",
                  }}
                >
                  {emotion && <span aria-hidden style={{ width: 8, height: 8, borderRadius: "50%", flexShrink: 0, background: emotionColor, boxShadow: `0 0 8px ${emotionColor}` }} />}
                  <span style={{ minWidth: 0, overflowWrap: "anywhere" }}>{emotion || "—"}</span>
                </div>
              </div>
            </div>
            <div style={{ flex: "1 1 200px", minWidth: 0 }}>
              <PillCloud
                items={ALL_EMOTIONS}
                active={emotion}
                label={(e) => <>{EMOTION_EMOJI[e]} {e}</>}
                accent={(e) => EMOTION_COLOR[e] ?? "#fff"}
                onPick={onEmotionPick}
                title={(e) => `Test emotion: ${e}`}
              />
            </div>
          </div>
        </div>
        )}

        {hasMotion && (
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Bot size={13} />} text="Servo Pose" /></div>
          {servo ? (
            <div style={{ display: "flex", flexWrap: "wrap" as const, alignItems: "flex-start", gap: 16 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 8, flex: "0 0 140px", minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-amber)" }}>
                  {servo.current || "idle"}
                  {(servo.bus_connected === false || servo.robot_connected === false) && (
                    <span style={{ fontSize: 10, color: "var(--lm-danger, #c44)", marginLeft: 6 }}>
                      (bus {servo.bus_connected === false ? "down" : "ok"}{servo.robot_connected === false ? ", robot off" : ""})
                    </span>
                  )}
                </div>
                <button className="lm-u-btn" onClick={onServoRelease} style={{
                  fontSize: 10, padding: "3px 9px", borderRadius: 6,
                  color: "var(--lm-text-dim)", alignSelf: "flex-start",
                }}>Release</button>
              </div>
              <div style={{ flex: "1 1 200px", minWidth: 0 }}>
                <PillCloud
                  items={servo.available_recordings ?? []}
                  active={servo.current ?? ""}
                  label={(p) => p}
                  accent={() => "var(--lm-amber)"}
                  onPick={onServoPlay}
                />
              </div>
            </div>
          ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
        </div>
        )}

        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 10 }}><CardLabel icon={<Tag size={13} />} text="Versions" /></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, overflowX: "auto" }}>
            <div className="lm-version-header" style={{ ...versionRowLayout, fontSize: 10, color: "var(--lm-text-muted)" }}>
              <span>Service</span>
              <span>Current</span>
              <span title="Latest published version in this device's OTA feed">Latest</span>
              <span style={{ textAlign: "right" }}>Uptime</span>
              <span />
              <span />
            </div>
            <VersionRow name="Host"   color="var(--lm-text)"   version={null}                    uptime={sys?.uptime ?? null}                                   updateTarget={null} />
            <VersionRow name="Web" latestVersion={otaVersions["web"]?.target}    color="var(--lm-teal)"   version={webVersion}              uptime={null}                                                  updateTarget={canUpdate("web") ? "web" : null} updating={isUpdating("web")} onTriggered={onUpdateTriggered} />
            <VersionRow restartTarget="os-server" name="OS" latestVersion={otaVersions["os-server"]?.target}     color="var(--lm-amber)"  version={sys?.version ?? null}    uptime={sys?.serviceUptime ?? null}                            updateTarget={canUpdate("os-server") ? "os-server" : null} updating={isUpdating("os-server")} onTriggered={onUpdateTriggered} />
            <VersionRow restartTarget="hal" name="HAL" latestVersion={otaVersions["hal"]?.target}    color="var(--lm-blue)"   version={halVersion}              uptime={sys?.halUptime ?? null}                                updateTarget={canUpdate("hal") ? "hal" : null} updating={isUpdating("hal")} onTriggered={onUpdateTriggered} />
            <VersionRow name="Agent" latestVersion={otaVersions["agent"]?.target}  color="var(--lm-purple)" version={oc?.version ?? null}     uptime={oc?.connected ? (oc?.agentUptime ?? null) : null}      updateTarget={canUpdate("agent") ? "agent" : null} updating={isUpdating("agent")} onTriggered={onUpdateTriggered} />
            {isDebug && <VersionRow name="Bootstrap" latestVersion={otaVersions["bootstrap"]?.target} color="var(--lm-text-dim)" version={otaVersions.bootstrap?.current ?? null} uptime={null} updateTarget={canUpdate("bootstrap") ? "bootstrap" : null} updating={isUpdating("bootstrap")} onTriggered={onUpdateTriggered} />}
            {isDebug && <VersionRow name="Device" latestVersion={otaVersions["device"]?.target} color="var(--lm-text-dim)" version={otaVersions.device?.current ?? null} uptime={null} updateTarget={canUpdate("device") ? "device" : null} updating={isUpdating("device")} onTriggered={onUpdateTriggered} />}
          </div>
        </div>
        </div>

        <div className="lm-cluster-col" style={{ order: 1 }}>
        <div className="lm-mon-card" style={monCard}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Cpu size={13} />} text="Hardware" />
            {ledColor && (
              <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <div style={{
                  width: 14, height: 14, borderRadius: "50%",
                  background: ledColor.on ? ledColor.hex : "transparent",
                  boxShadow: ledColor.on ? `0 0 8px ${ledColor.hex}cc` : "none",
                  border: `2px solid ${ledColor.on ? ledColor.hex : "var(--lm-border)"}`,
                  flexShrink: 0,
                }} title={`RGB(${ledColor.color.join(", ")})`} />
                <span style={{ fontSize: 10, fontFamily: "monospace", color: ledColor.on ? "var(--lm-text)" : "var(--lm-text-muted)" }}>
                  {ledColor.on ? ledColor.hex : "off"}
                </span>
                {ledColor.on && (
                  <span style={{ fontSize: 10, color: "var(--lm-text-dim)" }}>
                    {Math.round(ledColor.brightness * 100)}%
                  </span>
                )}
                {ledColor.effect && (
                  <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 4, background: "rgba(167,139,250,0.15)", color: "var(--lm-purple)", fontWeight: 600 }}>
                    {ledColor.effect}
                  </span>
                )}
                {ledColor.scene && !ledColor.effect && (
                  <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 4, background: "var(--lm-amber-dim)", color: "var(--lm-amber)", fontWeight: 600 }}>
                    {ledColor.scene}
                  </span>
                )}
              </div>
            )}
          </div>
          {hw ? (
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 7 }}>
              <HWBadge label="Servo" ok={hw.servo} />
              <HWBadge label="LED" ok={hw.led} />
              <HWBadge label="Camera" ok={hw.camera} />
              <HWBadge label="Audio" ok={hw.audio} />
              <HWBadge label="Sensing" ok={hw.sensing} />
              <HWBadge label="Voice" ok={hw.voice} />
              <HWBadge label="TTS" ok={hw.tts} />
            </div>
          ) : <SkeletonRows lines={2} />}
        </div>

        {/* Gated on data: the light capability cannot tell whether /scene exists. */}
        {sceneInfo && (
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Clapperboard size={13} />} text="Scene" /></div>
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 5 }}>
              {sceneInfo.scenes.map((s) => (
                <span key={s} role="button" onClick={() => onSceneActivate(s)} style={{
                  fontSize: 11,
                  padding: "3px 9px",
                  borderRadius: 6,
                  background: s === sceneInfo.active ? "var(--lm-amber-dim)" : "var(--lm-surface)",
                  border: `1px solid ${s === sceneInfo.active ? "var(--lm-amber)" : "var(--lm-border)"}`,
                  color: s === sceneInfo.active ? "var(--lm-amber)" : "var(--lm-text-dim)",
                  cursor: "pointer",
                  fontWeight: s === sceneInfo.active ? 600 : 400,
                  textTransform: "capitalize",
                }}>{s}</span>
              ))}
              <span role="button" onClick={() => onSceneActivate("off")} style={{
                fontSize: 11,
                padding: "3px 9px",
                borderRadius: 6,
                background: !sceneInfo.active ? "var(--lm-red)" : "var(--lm-surface)",
                border: `1px solid ${!sceneInfo.active ? "var(--lm-red)" : "var(--lm-border)"}`,
                color: !sceneInfo.active ? "#fff" : "var(--lm-text-dim)",
                cursor: "pointer",
                fontWeight: !sceneInfo.active ? 600 : 400,
              }}>Off</span>
            </div>
        </div>
        )}

        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Power size={13} />} text="Power" /></div>
          <DevicePowerButtons />
        </div>

        </div>
      </div>

      <div style={{ ...S.card, display: "none" }}>
        <div style={S.cardLabel}>Display Eyes</div>
        {displayState ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <StatusDot ok={displayState.hardware} />
              <span style={{ fontSize: 13, fontWeight: 600, color: "var(--lm-teal)" }}>{displayState.mode}</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap" as const, gap: 4 }}>
              {(displayState.available_expressions ?? []).map((e) => (
                <span key={e} style={{
                  fontSize: 10, padding: "2px 6px", borderRadius: 4,
                  background: e === displayState.mode ? "rgba(45,212,191,0.12)" : "var(--lm-surface)",
                  border: `1px solid ${e === displayState.mode ? "rgba(45,212,191,0.4)" : "var(--lm-border)"}`,
                  color: e === displayState.mode ? "var(--lm-teal)" : "var(--lm-text-dim)",
                }}>{e}</span>
              ))}
            </div>
          </div>
        ) : <span style={{ color: "var(--lm-text-muted)" }}>Loading…</span>}
      </div>

    </div>
  );
}

// Free-wrapping pill grid with the active item hoisted first.
function PillCloud<T extends string>({ items, active, label, accent, onPick, title }: {
  items: T[];
  active: string;
  label: (item: T) => ReactNode;
  accent: (item: T) => string;
  onPick: (item: T) => void;
  title?: (item: T) => string;
}) {
  const ordered = active && items.includes(active as T)
    ? [active as T, ...items.filter((i) => i !== active)]
    : items;
  return (
    <div className="lm-pillcloud">
      {ordered.map((item) => {
        const isActive = item === active;
        const c = accent(item);
        return (
          <span
            key={item}
            role="button"
            title={title?.(item)}
            onClick={() => onPick(item)}
            style={{
              fontSize: 10, padding: "2px 8px", borderRadius: 999,
              background: isActive ? `${c}22` : "var(--lm-surface)",
              border: `1px solid ${isActive ? c + "88" : "var(--lm-border)"}`,
              color: isActive ? c : "var(--lm-text-muted)",
              fontWeight: isActive ? 700 : 400,
              textTransform: "capitalize",
              transition: "all 0.2s ease",
              cursor: "pointer",
              whiteSpace: "nowrap",
            }}
          >
            {label(item)}
          </span>
        );
      })}
    </div>
  );
}

// HeroChip is a compact pill in the hero banner showing a single live stat (Agent / IP / Presence).
function HeroChip({ icon, label, value, tone }: {
  icon: ReactNode;
  label: string;
  value: string;
  tone: "ok" | "error" | "active" | "neutral";
}) {
  const color =
    tone === "ok" ? "var(--lm-green)" :
    tone === "error" ? "var(--lm-red)" :
    tone === "active" ? "var(--lm-amber)" :
    "var(--lm-text)";
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8,
      padding: "6px 12px", borderRadius: 10,
      background: "color-mix(in srgb, var(--lm-card) 70%, transparent)",
      border: "1px solid var(--lm-border)",
      backdropFilter: "blur(4px)",
    }}>
      <span style={{ display: "flex", color }} aria-hidden>{icon}</span>
      <span style={{ fontSize: 10, color: "var(--lm-text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
      <span style={{ fontSize: 12.5, fontWeight: 700, color, fontFamily: label === "IP" ? "monospace" : undefined }}>{value}</span>
    </div>
  );
}

// Perceptual VU mapping: mic RMS (int16) -> 0..100% via dBFS (-60 dBFS = 0%).
function micRmsToPct(rms: number): number {
  if (rms <= 0) return 0;
  const db = 20 * Math.log10(rms / 32768);
  return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
}

// A sensing-mic sample older than this is shown as dead air.
const NOISE_STALE_S = 60;

// Live VU meters from HAL's /voice/mic-level SSE stream (voice mic + sensing noise mic).
function MicLevelBar({ muted, onPlayback }: { muted: boolean; onPlayback?: (tts: boolean, music: boolean) => void }) {
  const fillRef = useRef<HTMLDivElement>(null);
  const noiseFillRef = useRef<HTMLDivElement>(null);
  const levelTextRef = useRef<HTMLSpanElement>(null);
  const noiseTextRef = useRef<HTMLSpanElement>(null);
  const [threshold, setThreshold] = useState<number | null>(null);
  const [noiseThreshold, setNoiseThreshold] = useState<number | null>(null);
  const [hasNoiseMic, setHasNoiseMic] = useState(false);
  // Forward playback state only on change so 10Hz frames never re-render the card.
  const onPlaybackRef = useRef(onPlayback);
  useEffect(() => { onPlaybackRef.current = onPlayback; }, [onPlayback]);
  const lastPlaybackRef = useRef<string>("");

  useEffect(() => {
    let es: EventSource | null = null;
    const open = () => {
      if (es || document.hidden) return;
      es = new EventSource(`${HW}/voice/mic-level`, { withCredentials: true });
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data) as {
            level: number; threshold: number; sensing_present?: boolean;
            sensing_level: number | null; sensing_age_s: number | null; sensing_threshold: number;
            tts_speaking?: boolean; music_playing?: boolean;
          };
          if (d.tts_speaking !== undefined || d.music_playing !== undefined) {
            const key = `${d.tts_speaking}|${d.music_playing}`;
            if (key !== lastPlaybackRef.current) {
              lastPlaybackRef.current = key;
              onPlaybackRef.current?.(d.tts_speaking ?? false, d.music_playing ?? false);
            }
          }
          if (fillRef.current) fillRef.current.style.width = `${micRmsToPct(d.level)}%`;
          if (levelTextRef.current) levelTextRef.current.textContent = String(Math.round(d.level));
          setThreshold((t) => (t === d.threshold ? t : d.threshold));
          const noiseLive = d.sensing_level != null && (d.sensing_age_s ?? Infinity) < NOISE_STALE_S;
          const present = d.sensing_present ?? d.sensing_level != null;
          setHasNoiseMic((h) => (h === present ? h : present));
          setNoiseThreshold((t) => (t === d.sensing_threshold ? t : d.sensing_threshold));
          if (noiseFillRef.current) {
            noiseFillRef.current.style.width = noiseLive ? `${micRmsToPct(d.sensing_level!)}%` : "0%";
          }
          if (noiseTextRef.current) {
            noiseTextRef.current.textContent = noiseLive ? String(Math.round(d.sensing_level!)) : "—";
          }
        } catch { /* malformed frame — skip */ }
      };
    };
    const close = () => { es?.close(); es = null; };
    // Gate on tab visibility so a background monitor tab doesn't hold the stream open (same pattern as the Logs section SSE).
    const onVis = () => (document.hidden ? close() : open());
    document.addEventListener("visibilitychange", onVis);
    open();
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      close();
    };
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
          <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Mic level</span>
          {muted ? (
            <span style={{ fontSize: 10, color: "var(--lm-text-muted)" }}>muted</span>
          ) : (
            <span title="live RMS / VAD threshold (speech must pass it to wake the robot)"
              style={{ fontSize: 11, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
              <span ref={levelTextRef}>0</span>{threshold != null ? ` / ${threshold}` : ""}
            </span>
          )}
        </div>
        <LevelTrack fillRef={fillRef} dim={muted}
          tick={muted ? null : threshold} tickTitle="VAD threshold — speech must pass this level to wake the robot" />
      </div>
      {hasNoiseMic && (
        <div>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Noise mic</span>
            <span title="last sample RMS / loud-noise threshold (samples past it startle the robot)"
              style={{ fontSize: 11, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
              <span ref={noiseTextRef}>—</span>{noiseThreshold != null ? ` / ${noiseThreshold}` : ""}
            </span>
          </div>
          <LevelTrack fillRef={noiseFillRef} dim={false} slow
            tick={noiseThreshold} tickTitle="Loud-noise threshold — samples past this level raise a sound event" />
        </div>
      )}
    </div>
  );
}

// One VU track; the fill is driven externally via `fillRef` (no re-render).
function LevelTrack({ fillRef, dim, tick, tickTitle, slow }: {
  fillRef: React.RefObject<HTMLDivElement | null>;
  dim: boolean;
  tick: number | null;
  tickTitle: string;
  slow?: boolean;
}) {
  return (
    <div style={{ position: "relative", height: 6, borderRadius: 999, background: "var(--lm-surface)", opacity: dim ? 0.5 : 1 }}>
      <div
        ref={fillRef}
        style={{
          position: "absolute", left: 0, top: 0, bottom: 0, width: "0%",
          borderRadius: 999,
          background: "linear-gradient(90deg, var(--lm-green), var(--lm-amber))",
          transition: slow ? "width 0.4s ease" : "width 0.1s linear",
        }}
      />
      {tick != null && tick > 0 && (
        <div
          title={tickTitle}
          style={{
            position: "absolute", left: `${micRmsToPct(tick)}%`, top: -2, bottom: -2,
            width: 2, borderRadius: 1, background: "var(--lm-amber)", opacity: 0.7,
          }}
        />
      )}
    </div>
  );
}

// Placeholder matching the Audio card layout while voice status loads.
function AudioSkeleton() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {[0, 1, 2].map((i) => (
        <div key={i} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Skeleton width={7} height={7} style={{ borderRadius: "50%" }} />
            <Skeleton width={54} height={12} />
          </div>
          <Skeleton width={60} height={24} style={{ borderRadius: 6 }} />
        </div>
      ))}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Skeleton width="40%" height={11} />
        <Skeleton width="100%" height={6} style={{ borderRadius: 999 }} />
      </div>
    </div>
  );
}

// ToggleButton is the small Mute/Unmute/Stop control in the Audio card.
function ToggleButton({ active, label, onClick, disabled = false }: {
  active: boolean;
  label: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  const tone = active ? STATUS_TONE.error : STATUS_TONE.ok;
  return (
    <button className="lm-u-btn" onClick={onClick} disabled={disabled} style={{
      fontSize: 11, padding: "5px 14px", borderRadius: 6, fontWeight: 600,
      background: tone.bg, border: `1px solid ${tone.border}`, color: tone.color,
      opacity: disabled ? 0.45 : 1, cursor: disabled ? "not-allowed" : "pointer",
    }}>
      {label}
    </button>
  );
}

const versionRowLayout = {
  display: "grid",
  gridTemplateColumns: "70px minmax(55px, 1fr) minmax(55px, 1fr) 70px 70px 65px",
  minWidth: 425,
  alignItems: "center",
  gap: 8,
};

function VersionRow({ name, color, version, latestVersion, uptime, updateTarget, updating = false, onTriggered, restartTarget }: {
  name: string;
  color: string;
  version: string | null;
  latestVersion?: string;
  uptime: number | null;
  updateTarget: "os-server" | "bootstrap" | "web" | "hal" | "device" | "agent" | null;
  restartTarget?: "os-server" | "hal";
  // Show a label during install; without it operators press again, which can break the runtime.
  updating?: boolean;
  onTriggered?: (target: string) => void;
}) {
  return (
    <div className="lm-version-row" style={versionRowLayout}>
      <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>{name}</span>
      <span data-label="Current" title={version ?? undefined} style={{ fontSize: 12.5, fontWeight: 600, color, fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{version ?? "—"}</span>
      <span data-label="Latest" title={latestVersion || undefined} style={{ fontSize: 12.5, color: "var(--lm-text-dim)", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{latestVersion || "—"}</span>
      <span data-label="Uptime" style={{ fontSize: 11, color: "var(--lm-text-muted)", textAlign: "right" }}>
        {uptime != null ? formatUptime(uptime) : "—"}
      </span>
      <span style={{ display: "flex", justifyContent: "flex-end" }}>
        {updating
          ? <span style={{ fontSize: 9.5, fontWeight: 600, color: "var(--lm-amber)" }} title="Installing — the component restarts when it finishes">updating…</span>
          : updateTarget && <SoftwareUpdateButton target={updateTarget} label="update" onTriggered={onTriggered} />}
      </span>
      <span style={{ display: "flex", justifyContent: "flex-end" }}>
        {restartTarget && <RestartServiceButton target={restartTarget} disabled={updating} />}
      </span>
    </div>
  );
}
