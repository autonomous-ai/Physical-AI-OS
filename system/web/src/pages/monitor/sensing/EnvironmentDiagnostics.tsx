import { ChevronRight, Cpu } from "lucide-react";
import type { EnvironmentComponentStatus, EnvironmentStatus } from "./environmentApi";

const stateLabels: Record<EnvironmentComponentStatus["state"], string> = {
  ready: "Receiving data", starting: "Connecting", error: "Sensor error", disabled: "Disabled", stopped: "Stopped",
};

function ComponentDetails({ name, status }: { name: string; status: EnvironmentComponentStatus }) {
  const timestamp = status.sample?.timestamp;
  const date = timestamp != null && Number.isFinite(timestamp) ? new Date(timestamp * 1000) : null;
  const label = status.stale && status.state === "ready" ? "Stale data" : stateLabels[status.state];
  const tone = status.state === "ready" && !status.stale ? "ready" : status.last_error || status.state === "error" || status.stale && status.state === "ready" ? "warning" : "neutral";
  const timing = [
    ["Poll interval", status.timing?.poll_interval_s],
    ["Retry interval", status.timing?.retry_interval_s],
    ["Stale after", status.timing?.stale_after_s],
    ["No-data timeout", status.timing?.no_data_timeout_s],
  ] as const;
  return <article className="lm-sensor-component">
    <header>
      <h3><Cpu size={16} aria-hidden="true" />{name}</h3>
      <span className={`lm-sensor-status lm-sensor-status--${tone}`}>{label}</span>
    </header>
    {status.last_error && <p className="lm-sensor-error" role="alert">{status.last_error}</p>}
    <dl className="lm-sensor-properties">
      <div><dt>I²C bus</dt><dd>{status.bus ?? "—"}</dd></div>
      {status.sample?.device_status != null && <div>
        <dt>Status register</dt><dd><code>{`0x${status.sample.device_status.toString(16).padStart(8, "0")}`}</code></dd>
      </div>}
      <div><dt>Last measurement</dt><dd>{date ? <time dateTime={date.toISOString()}>
        {date.toLocaleTimeString()}<span className="lm-sensor-date">{date.toLocaleDateString()}</span>
      </time> : "No sample yet"}</dd></div>
    </dl>
    <div className="lm-sensor-timing-label">Timing</div>
    <dl className="lm-sensor-properties">
      {timing.map(([title, seconds]) => <div key={title}>
        <dt>{title}</dt><dd>{seconds != null ? <>{seconds}<span className="lm-sensor-unit">s</span></> : "—"}</dd>
      </div>)}
    </dl>
  </article>;
}

export function EnvironmentDiagnostics({ data }: { data: EnvironmentStatus | null }) {
  const components = data ? data.components ? Object.entries(data.components) : [["Environment sensor", data] as const] : [];
  const disabled = components.filter(([, status]) => (status.enabled === false || status.state === "disabled") && !status.last_error);
  const active = components.filter((entry) => !disabled.includes(entry));
  const card = ([name, status]: (typeof components)[number]) => <ComponentDetails key={name} name={data?.components ? name.toUpperCase() : name} status={status} />;
  return <details className="lm-sensor-details">
    <summary><ChevronRight size={16} aria-hidden="true" /><span>Sensor details</span><span className="lm-sensor-count">{components.length}</span></summary>
    <p className="lm-sensor-intro">Connection status and reporting intervals for each sensor.</p>
    {active.length > 0 && <div className="lm-sensor-grid">{active.map(card)}</div>}
    {disabled.length > 0 && <details className="lm-sensor-disabled">
      <summary><ChevronRight size={14} aria-hidden="true" /><span>Disabled sensors</span><span className="lm-sensor-count">{disabled.length}</span></summary>
      <div className="lm-sensor-grid">{disabled.map(card)}</div>
    </details>}
    {components.length === 0 && <p className="lm-sensor-intro">No sensor details available yet.</p>}
  </details>;
}
