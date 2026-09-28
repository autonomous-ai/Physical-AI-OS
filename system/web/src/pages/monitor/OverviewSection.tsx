import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Satellite, Globe, Eye, Volume2, Cpu, Drama, Clapperboard, Bot, Tag, Wifi, LayoutDashboard, Power } from "lucide-react";
import { S } from "./styles";
import { API, HW } from "./types";

const EMOTION_EMOJI: Record<string, string> = {
  happy: "😊", curious: "🤔", thinking: "💭", sad: "😢", excited: "🤩",
  shy: "😳", shock: "😱", idle: "😐", listening: "👂", laugh: "😄",
  confused: "😕", sleepy: "😴", greeting: "👋", goodbye: "👋", acknowledge: "👍",
  stretching: "🙆", caring: "🤗", music_chill: "🎵", music_strong: "🎸",
  scan: "👀", nod: "👍", headshake: "🙅",
};

type OtaVersions = Record<string, { current?: string; target?: string; update_available?: boolean }>;

// Owned by the monitor, so tab unmounts retain successful metadata snapshots.
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
    // Bootstrap down → no buttons, same as nothing to update.
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
  hasEmotion: boolean; // device declares the expression capability (/emotion route)
  hasMotion: boolean; // device declares the motion capability (/servo route)
  webVersion: string;
  halVersion: string | null;
  onSceneActivate: (scene: string) => void;
  // Action callbacks live in the parent (same as onSceneActivate): it owns the
  // polled state, so it can commit the new value the moment HAL/os-server acks
  // the POST instead of leaving the control frozen until the next poll tick.
  onMicMutedChange: (muted: boolean) => void;
  onSpeakerMutedChange: (muted: boolean) => void;
  onTTSStop: () => void;
  // Live playback state off the mic-level SSE stream (~10Hz) — lets the parent
  // flip tts_speaking/musicPlaying the moment playback ends instead of waiting
  // out the 5s /voice/status poll.
  onPlaybackLive?: (tts: boolean, music: boolean) => void;
  onEmotionPick: (emotion: string) => void;
  onServoPlay: (recording: string) => void;
  onServoRelease: () => void;
}) {
  const { emotions: ALL_EMOTIONS, colors: EMOTION_COLOR } = useEmotionPresets(cache);
  const emotion = oc?.emotion ?? "";
  const emotionColor = EMOTION_COLOR[emotion] ?? "var(--lm-text-muted)";
  const emotionEmoji = EMOTION_EMOJI[emotion] ?? "✦";

  // Software-update buttons in the Versions card are gated behind ?debug=true
  // so the regular monitor view doesn't ship one-click OTA triggers (rate
  // limit + admin auth still apply on the server side either way).
  const isDebug = new URLSearchParams(window.location.search).get("debug") === "true";

  // Show published versions in normal mode; only update actions require debug.
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
  // held_by_floor is deliberately NOT consulted: that floor stages the
  // automatic fleet rollout, while this button installs the published version
  // on this one device — the same thing `software-update <key>` over SSH has
  // always done.
  const canUpdate = (target: string) => isDebug && !!otaVersions[target]?.update_available;

  // Which rows are installing right now. Polled from the cheap /ota-updating
  // (no metadata fetch) every 2s while something runs, and every 10s otherwise
  // so a card opened mid-update — or an update started from another tab or over
  // SSH — still shows the label. When the set empties, re-read /ota-versions so
  // the row picks up its new version and the button disappears.
  const [otaUpdating, setOtaUpdating] = useState<string[]>([]);
  const otaUpdatingRef = useRef<string[]>([]);
  // Optimistic overlay: keyed by target, value = when the click was accepted.
  // Held for a few seconds so a component that installs faster than one poll
  // still flashes the label instead of jumping straight to "done".
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
    // Let a click jump the queue instead of waiting out the idle interval.
    pokePollRef.current = () => { if (timer) clearTimeout(timer); void poll(); };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [refreshOtaVersions]);

  const onUpdateTriggered = useCallback((target: string) => {
    setJustTriggered((prev) => ({ ...prev, [target]: Date.now() }));
    pokePollRef.current();
    // Clear the overlay after the grace window and re-read versions: by then
    // either the server reports it (label stays via otaUpdating) or it is done.
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

  // Volume slider: local state for smooth dragging, API call only on release
  const [localVolume, setLocalVolume] = useState<number | null>(null);
  const draggingVolume = useRef(false);
  // Reactive mirror of draggingVolume so render can decide between the exact
  // handle value (mid-drag) and the eased count-up (idle) without reading a ref.
  const [dragging, setDragging] = useState(false);

  // Sync from server when not dragging
  useEffect(() => {
    if (!draggingVolume.current && audio?.volume != null) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- the slider is an uncontrolled-while-dragging input: server volume may only overwrite it BETWEEN drags. Deriving it during render would yank the handle out from under the operator's finger mid-drag.
      setLocalVolume(audio.volume);
    }
  }, [audio?.volume]);

  // Animated stats: link rate and volume tick to their new value instead of
  // snapping, so a refresh reads as a live needle rather than a hard cut.
  const animatedLinkRate = useCountUp(net?.linkRate ?? 0);
  const animatedVolume = useCountUp(localVolume ?? audio?.volume ?? 0);

  // Slider ceiling: SAFETY.md `audio.max_volume` when the device declares one,
  // else the full scale. The gate in HAL is what actually enforces this — the
  // slider only stops short of a dead zone that would snap the handle back.
  const volumeCeiling = audio?.max_volume ?? 100;
  const volumeValue = Math.min(localVolume ?? audio?.volume ?? 50, volumeCeiling);
  // The track's amber fill is a fraction of the track's own width, so it is
  // relative to the ceiling, not to 100.
  const volumeFillPct = volumeCeiling > 0 ? (volumeValue / volumeCeiling) * 100 : 0;

  const commitVolume = useCallback((vol: number) => {
    draggingVolume.current = false;
    setDragging(false);
    fetch(`${HW}/audio/volume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ volume: vol }),
    })
      // Snap to what HAL actually applied. The slider already stops at the
      // ceiling, but a stale ceiling (SAFETY.md changed under a long-open tab)
      // would otherwise leave the handle showing a value the device rejected
      // until the next poll — which reads as the control being broken.
      .then((r) => r.json())
      .then((r) => {
        if (typeof r?.volume === "number") setLocalVolume(r.volume);
      })
      .catch(() => {});
  }, []);

  // Base card style for the Overview: the `.lm-mon-card` class owns the
  // resting + hover box-shadow (and the gradient/accent/glow), so we strip the
  // inline boxShadow from S.card to let the class's :hover shadow win.
  const monCard = { ...S.card, boxShadow: undefined };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>

      {/* Hero banner — a command-center header summarizing live device state.
          Read-only: every chip reflects an already-fetched prop, no new calls. */}
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
              value={oc?.connected ? "Online" : "Offline"}
              tone={oc?.connected ? "ok" : "error"}
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

      {/* Row 1: 4 status cards in one row */}
      <div className="lm-grid-4 lm-overview-status-grid">
        {/* Agent Gateway */}
        {/* position:relative anchors the bottom-right RestartAgentButton to the card. */}
        <div className="lm-mon-card" style={{ ...monCard, position: "relative" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
            <CardLabel icon={<Satellite size={13} />} text="Agent Gateway" />
            <StatusBadge text={oc?.connected ? "ONLINE" : "OFFLINE"} ok={!!oc?.connected} pulse={!!oc?.connected} />
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

        {/* Network */}
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

        {/* Presence */}
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

        {/* Audio */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Volume2 size={13} />} text="Audio" /></div>
          {voice ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {/* Mic row */}
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
                {/* HW slide switch is the authority: while it's off, /voice/unmute
                    returns 409 (see routes/voice.py). Disable the toggle and show
                    a text hint under it so the user knows to flip the physical
                    switch. hw_mic_switch_muted === null → device has no switch
                    (Lamp) → normal behavior. */}
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

              {/* TTS row */}
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

              {/* Speaker row */}
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

              {/* Volume slider */}
              <div>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
                  <span style={{ fontSize: 12.5, fontWeight: 600, color: "var(--lm-text-dim)" }}>Volume</span>
                  <span style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
                    {/* Why the slider stops short of 100 — without this the
                        ceiling reads as a broken control. */}
                    {audio?.max_volume != null && (
                      <span
                        style={{ fontSize: 11, fontWeight: 600, color: "var(--lm-text-dim)" }}
                        title="Speaker ceiling from this robot's SAFETY.md (audio.max_volume). Enforced in HAL for every caller, not just this slider."
                      >
                        ceiling {audio.max_volume}%
                      </span>
                    )}
                    <span style={{ fontSize: 14, fontWeight: 700, color: "var(--lm-amber)", fontFamily: "monospace" }}>
                      {/* While dragging show the exact handle value (no easing lag);
                          when idle let it tick to the server-confirmed value. */}
                      {dragging ? (localVolume ?? audio?.volume ?? "—") : animatedVolume}%
                    </span>
                  </span>
                </div>
                <input
                  type="range"
                  min={0}
                  max={volumeCeiling}
                  value={volumeValue}
                  onChange={(e) => {
                    draggingVolume.current = true;
                    setDragging(true);
                    setLocalVolume(Number(e.target.value));
                  }}
                  onMouseUp={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  onTouchEnd={(e) => commitVolume(Number((e.target as HTMLInputElement).value))}
                  className="lm-mon-range"
                  style={{
                    width: "100%", cursor: "pointer",
                    // Drives the amber-fill width in the .lm-mon-range track (paint only).
                    ["--lm-fill" as string]: `${volumeFillPct}%`,
                  }}
                />
              </div>

              {/* Live mic input VU meter */}
              <MicLevelBar muted={voice.mic_muted ?? false} onPlayback={onPlaybackLive} />
            </div>
          ) : <AudioSkeleton />}
        </div>
      </div>

      {/* Row 2: device & capability cluster, split into two masonry columns so
          cards pack by their natural height instead of being forced to equal
          height by one auto-fit grid (which left dead space in the compact cards
          and cramped the tall pill-cloud ones). The RIGHT column carries the
          expressive cards (Emotion + Servo pose), each with a scrollable pill
          cloud; the LEFT column carries the compact status cards (Hardware,
          Scene, Versions, Buddy). DOM order keeps the expressive cards first but
          CSS `order` paints them on the right. On a minimal device the capability-gated cards
          simply don't render, and the remaining cards still read cleanly. On a
          narrow viewport the two columns collapse to one (see .lm-cluster). */}
      <div className="lm-cluster">
        {/* Expressive cards (Emotion + Servo) — painted on the RIGHT via order: 2 */}
        <div className="lm-cluster-col" style={{ order: 2 }}>
        {/* Emotion — only for devices that declare the expression capability (the
            /emotion route). intern-v2 has no expression, so the whole card is hidden
            rather than showing an agent emotion the device can't actually express. */}
        {hasEmotion && (
        <div style={{
          ...S.card, padding: "14px 16px",
          background: emotion ? `linear-gradient(135deg, var(--lm-bg) 60%, ${emotionColor}18)` : "var(--lm-bg)",
          border: `1px solid ${emotion ? emotionColor + "55" : "var(--lm-border)"}`,
          transition: "all 0.4s ease",
        }}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Drama size={13} />} text="Emotion" /></div>
          {/* Two-column body: the current-emotion summary (emoji + name) sits in
              a fixed-width left column, and the full preset list wraps as a tidy
              cloud filling the rest on the right — instead of stacking the cloud
              full-width under the summary. Wraps to stacked under ~360px. */}
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
                {/* Keep the state name on the theme's high-contrast text colour.
                    Preset colours can be deliberately dark (e.g. sleepy), so
                    they are an accent dot and border instead of the text itself. */}
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

        {/* Servo — only for devices that declare the motion capability (e.g. lamp, not intern-v2) */}
        {hasMotion && (
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Bot size={13} />} text="Servo Pose" /></div>
          {servo ? (
            // Two-column body mirroring the Emotion card: the current pose +
            // Release control sit in a fixed-width left column, and the recording
            // list wraps as a tidy cloud filling the rest on the right. Wraps to
            // stacked under ~360px.
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

        {/* Versions — kept in the expressive (right) column so the two columns
            balance in height: Hardware + Scene + Buddy on the left roughly match
            Emotion + Servo + Versions on the right, instead of the right column
            ending short under Servo Pose. OS uptime sits in the host row;
            detailed CPU/RAM/Disk live in the System tab. */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 10 }}><CardLabel icon={<Tag size={13} />} text="Versions" /></div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, overflowX: "auto" }}>
            <div style={{ ...versionRowLayout, fontSize: 10, color: "var(--lm-text-muted)" }}>
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

        {/* Compact status cards — painted on the LEFT via order: 1 */}
        <div className="lm-cluster-col" style={{ order: 1 }}>
        {/* Hardware */}
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

        {/* Scene — `scene` is a route WITHIN the `light` capability (lamp declares
            light:[led,scene]; intern-v2 declares light:[led] only), so the capability
            list can't distinguish it. Gate on data instead: a device whose light
            capability omits the scene route leaves /scene 404ing, so sceneInfo stays
            null and the card never renders (rather than showing "Loading…" forever). */}
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

        {/* Power actions are intentionally routed through os-server: it applies
            admin auth, acknowledges the browser, then invokes HAL's full
            cue-aware reboot/shutdown sequence. */}
        <div className="lm-mon-card" style={monCard}>
          <div style={{ marginBottom: 12 }}><CardLabel icon={<Power size={13} />} text="Power" /></div>
          <DevicePowerButtons />
        </div>

        </div>
      </div>

      {/* Display Eyes — hidden via display:none, code kept for future re-enable */}
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

// PillCloud renders a de-cluttered, free-wrapping grid of selectable pills for
// the Emotion / Servo cards. Both previously dumped every preset (20+ emotions,
// 30+ poses) as tiny tags that buried the card and read as noise. Here the active
// pill is hoisted to the front so the current state always reads first, and the
// whole set wraps as a tidy cloud (no scroll). Pills keep their per-item accent
// color.
function PillCloud<T extends string>({ items, active, label, accent, onPick, title }: {
  items: T[];
  active: string;
  label: (item: T) => ReactNode;
  accent: (item: T) => string;
  onPick: (item: T) => void;
  title?: (item: T) => string;
}) {
  // Active item first so the current state always reads first.
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

// HeroChip is a compact pill in the hero banner showing a single live stat
// (Agent / IP / Presence). Presentational only — values are passed in.
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

// Perceptual VU mapping: raw mic RMS (int16 scale, 0..32768) → 0..100% via
// dBFS so quiet-room noise sits low and speech visibly pumps the bar.
// -60dBFS (RMS ≈ 33) → 0%, 0dBFS (full scale) → 100%.
function micRmsToPct(rms: number): number {
  if (rms <= 0) return 0;
  const db = 20 * Math.log10(rms / 32768);
  return Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
}

// A sensing-mic sample older than this is shown as dead air (bar at 0): the
// sensing loop samples every few seconds and pauses during/after TTS, so a
// minute without a fresh sample means sound perception isn't really running.
const NOISE_STALE_S = 60;

// MicLevelBar renders the live VU meters under the volume slider from HAL's
// `/voice/mic-level` SSE stream (~10Hz, via the /api/hardware proxy):
// - "Mic level" — the voice-pipeline (STT) mic; pumps as the user talks into
//   the device, same idea as the vu-bar in the Gemini test page but reading
//   the DEVICE mic, not the browser's.
// - "Noise mic" — the sensing mic (SoundPerception); one 0.5s RMS sample per
//   sensing poll, so this bar steps every few seconds rather than pumping.
//   Hidden entirely when the device has no sound perception running.
// Fills are mutated directly through refs (no state) so 10Hz updates never
// re-render the Audio card; amber ticks mark each pipeline's trigger
// threshold (VAD wake / loud-noise). The stream stays open while the voice
// mic is muted — the sensing mic is independent of the mute switch.
function MicLevelBar({ muted, onPlayback }: { muted: boolean; onPlayback?: (tts: boolean, music: boolean) => void }) {
  const fillRef = useRef<HTMLDivElement>(null);
  const noiseFillRef = useRef<HTMLDivElement>(null);
  // Numeric readouts (raw RMS) mutated via refs like the fills — no re-render.
  const levelTextRef = useRef<HTMLSpanElement>(null);
  const noiseTextRef = useRef<HTMLSpanElement>(null);
  const [threshold, setThreshold] = useState<number | null>(null);
  const [noiseThreshold, setNoiseThreshold] = useState<number | null>(null);
  const [hasNoiseMic, setHasNoiseMic] = useState(false);
  // Playback state piggybacked on the stream: forward to the parent only on
  // CHANGE (ref-compared) so 10Hz frames never re-render the Audio card. Ref
  // for the callback keeps the once-mounted SSE effect free of stale closures.
  const onPlaybackRef = useRef(onPlayback);
  // Keep the ref current from an effect rather than during render: the ref is
  // only ever read from the async SSE handler, so a post-commit write is
  // equivalent and keeps render side-effect free.
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
          // Render the noise bar as soon as sound perception exists — it sits
          // at 0 until the first sample lands (one per sensing poll).
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
      // EventSource auto-reconnects on transient errors; nothing to do here.
    };
    const close = () => { es?.close(); es = null; };
    // Gate on tab visibility so a background monitor tab doesn't hold the
    // stream open (same pattern as the Logs section SSE).
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
          {/* Slow transition: one sample per sensing poll → ease between steps */}
          <LevelTrack fillRef={noiseFillRef} dim={false} slow
            tick={noiseThreshold} tickTitle="Loud-noise threshold — samples past this level raise a sound event" />
        </div>
      )}
    </div>
  );
}

// One VU track: surface-colored rail, green→amber gradient fill driven
// externally via `fillRef` (direct style writes, no re-render), optional
// amber threshold tick.
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

// AudioSkeleton mirrors the Audio card's layout (three labelled toggle rows +
// a volume slider) so the card holds its height while voice status loads,
// avoiding the jump the old "Loading…" text caused.
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

// ToggleButton is the small Mute/Unmute/Stop control in the Audio card. When
// `active` (e.g. mic live) it shows a destructive red tone; when inactive
// (already muted) it offers a green "Unmute". Tones come from STATUS_TONE so
// they stay theme-aware (the old code hardcoded #f87171 which broke on light).
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
  // An install runs for tens of seconds (the component stops, is rebuilt and
  // restarts). Without a label the row just sits there — on HAL it even shows a
  // stale version while /opt/hal does not exist — and the natural reaction is to
  // press the button again, which is how a device lost its runtime.
  updating?: boolean;
  onTriggered?: (target: string) => void;
}) {
  // Keep both action columns aligned, with horizontal scrolling on narrow cards.
  return (
    <div style={versionRowLayout}>
      <span style={{ fontSize: 12.5, color: "var(--lm-text-dim)" }}>{name}</span>
      <span title={version ?? undefined} style={{ fontSize: 12.5, fontWeight: 600, color, fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{version ?? "—"}</span>
      <span title={latestVersion || undefined} style={{ fontSize: 12.5, color: "var(--lm-text-dim)", fontFamily: "monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{latestVersion || "—"}</span>
      <span style={{ fontSize: 11, color: "var(--lm-text-muted)", textAlign: "right" }}>
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
