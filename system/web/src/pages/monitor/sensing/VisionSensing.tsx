import type { ReactNode } from "react";
import { Activity, AudioLines, Eye, PersonStanding, ScanFace, Sun, Users } from "lucide-react";
import { MotionCard } from "./MotionCard";
import { EmotionCard } from "./EmotionCard";
import { FaceCard } from "./FaceCard";
import { PresenceCard } from "./PresenceCard";
import { LightCard } from "./LightCard";
import { SoundCard } from "./SoundCard";
import { BackendCard } from "./BackendCard";
import { EventsCard } from "./EventsCard";
import { PoseCard } from "./PoseCard";
import { Pill } from "./CardHeader";
import { fmtAgo, lightTier, presenceColor, riskName } from "./format";
import { useVisionSensing } from "./useVisionSensing";

function Reading({ icon, title, value, detail }: { icon: ReactNode; title: string; value: string; detail: string }) {
  return <article className="lm-sensing-reading">
    <div className="lm-sensing-reading-label">{icon}<h3>{title}</h3></div>
    <div className="lm-sensing-reading-value">{value}</div>
    <p>{detail}</p>
  </article>;
}

export function VisionSensing() {
  const { data, error } = useVisionSensing();
  if (error) return <section className="lm-sensing-message" role="alert">
    <Activity size={22} aria-hidden="true" />
    <div><h2>Sensing is unavailable</h2><p>{error} The page will retry automatically.</p></div>
  </section>;
  if (!data) return <section className="lm-sensing-message" role="status">
    <Activity size={22} aria-hidden="true" />
    <div><h2>Connecting to sensors</h2><p>Waiting for the robot’s latest observations.</p></div>
  </section>;
  const perception = (type: string) => data.perceptions.find((item) => item.type === type);
  const motion = perception("motion");
  const face = perception("face");
  const light = perception("light_level");
  const sound = perception("sound");
  const emotion = perception("emotion");
  const pose = perception("pose");
  const state = data.presence.enabled ? data.presence.state : "off";
  const presenceLabels: Record<string, string> = { present: "Presence detected", active: "Activity detected", disabled: "Presence sensing is off", idle: "No recent activity", away: "Away", off: "Presence sensing is off" };
  const names = face?.visible ?? [];
  const recentMotion = motion?.seconds_since_motion != null && motion.seconds_since_motion < 30;
  const lightLevel = light?.level;
  const hasLight = lightLevel != null && Number.isFinite(lightLevel);
  return <>
    <section className="lm-sensing-hero">
      <div className="lm-sensing-hero-top">
        <span className="lm-sensing-eyebrow"><Activity size={15} aria-hidden="true" /> AROUND YOUR ROBOT</span>
        <Pill text={data.running ? "Updating" : "Paused"} color={data.running ? "var(--lm-green)" : "var(--lm-text-muted)"} />
      </div>
      <h2>{presenceLabels[state] ?? "Waiting for presence data"}</h2>
      <p>{!data.running ? "Sensing is paused. Readings below are the last reported observations."
        : data.presence.enabled ? "Presence, people and changes in the room — at a glance."
        : "Other available observations are shown below."}</p>
      <div className="lm-sensing-hero-footer">
        <span><i style={{ background: presenceColor(state) }} />Presence: {state || "unknown"}</span>
        {data.presence.enabled && <span>Last motion: {fmtAgo(data.presence.seconds_since_motion)}</span>}
      </div>
    </section>
    <div className="lm-sensing-readings">
      <Reading icon={<Users size={18} />} title="People in view"
        value={!face ? "No data" : names.length ? `${names.length} visible` : "No faces detected"}
        detail={!face ? "Waiting for a face observation." : names.length ? names.join(", ") : face.last_person ? `Last seen: ${face.last_person} · ${fmtAgo(face.last_seen_seconds_ago)}` : "No recent person to show."} />
      <Reading icon={<Activity size={18} />} title="Movement"
        value={!motion || motion.seconds_since_motion == null ? "No data" : recentMotion ? "Recent movement" : "No recent movement"}
        detail={motion?.seconds_since_motion != null ? `Last detected ${fmtAgo(motion.seconds_since_motion)}.` : "Waiting for a movement observation."} />
      <Reading icon={<Sun size={18} />} title="Room light"
        value={hasLight ? lightTier(lightLevel).label : "No data"}
        detail={hasLight ? `Sensor level ${Math.round(lightLevel)} · checked ${fmtAgo(light?.seconds_since_check)}` : "Waiting for a light reading."} />
      <Reading icon={<AudioLines size={18} />} title="Sound events"
        value={sound?.occurrence_count != null ? String(sound.occurrence_count) : "No data"}
        detail={sound ? "Reported sound occurrences, not a loudness measurement." : "Waiting for a sound observation."} />
    </div>
    {(emotion || pose) && <div className="lm-sensing-secondary">
      {emotion && <Reading icon={<ScanFace size={18} />} title="Detected expression"
        value={emotion.last_detected_emotion || "No detection yet"}
        detail={`Visual estimate${emotion.seconds_since_detection != null ? ` · ${fmtAgo(emotion.seconds_since_detection)}` : ""}. This may not reflect how someone feels.`} />}
      {pose && <Reading icon={<PersonStanding size={18} />} title="Posture"
        value={pose.ergo_risk_level != null ? `${riskName(pose.ergo_risk_level)} risk` : "Collecting observations"}
        detail={pose.ergo_score != null ? `Latest score ${pose.ergo_score} · ${fmtAgo(pose.seconds_since_sample)}` : "A posture estimate will appear when a sample is available."} />}
    </div>}
    <details className="lm-sensing-details">
      <summary><span><Eye size={17} aria-hidden="true" />Technical details</span><span className="lm-sensing-details-hint">Connections, events & samples</span></summary>
      <div className="lm-sensing-diagnostics">
        <PresenceCard data={data} /><MotionCard motion={motion} /><FaceCard face={face} /><EmotionCard emotion={emotion} />
        <LightCard light={light} /><SoundCard sound={sound} /><BackendCard data={data} /><EventsCard ev={data.last_event_seconds_ago} />
      </div>
      {pose && <PoseCard pose={pose} />}
    </details>
  </>;
}
