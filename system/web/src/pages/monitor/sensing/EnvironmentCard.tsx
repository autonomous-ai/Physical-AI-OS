import { S } from "../styles";
import type { Measurement } from "./environmentApi";
import { useEnvironment } from "./useEnvironment";
import { EnvironmentDiagnostics } from "./EnvironmentDiagnostics";

const measurements: [Measurement, string, string][] = [
  ["temperature_c", "Temperature", "°C"], ["humidity_pct", "Humidity", "%RH"],
  ["co2_ppm", "CO₂", "ppm"], ["pm2_5_ug_m3", "PM2.5", "µg/m³"],
  ["pm1_0_ug_m3", "PM1", "µg/m³"],
  ["pm4_0_ug_m3", "PM4", "µg/m³"], ["pm10_ug_m3", "PM10", "µg/m³"],
  ["voc_index", "VOC", "index"], ["nox_index", "NOx", "index"],
];
const stateLabels = { disabled: "Disabled", starting: "Connecting", ready: "Receiving data", error: "Sensor error", stopped: "Stopped" };

export function EnvironmentCard({ available }: { available: boolean }) {
  const { data, error } = useEnvironment(available);

  const stale = !!error || data?.stale !== false;
  const sampleTimestamp = data?.sample?.timestamp;
  const hasSampleTimestamp = sampleTimestamp != null && Number.isFinite(sampleTimestamp);
  const hasGasIndexes = !!data?.sources?.voc_index || !!data?.sources?.nox_index
    || data?.sample?.voc_index != null || data?.sample?.nox_index != null;
  const componentIssues = Object.entries(data?.components ?? {}).filter(([, status]) => status.enabled !== false && status.state !== "disabled" && (status.stale || status.last_error));
  const label = !available ? "Not available" : error ? "Unavailable" : !data ? "Loading…" : data.stale && data.state === "ready" ? "Stale data" : stateLabels[data.state];
  const showMeasurements = available && data?.state !== "disabled" && !!data?.sample;
  const renderMeasurement = ([key, title, unit]: [Measurement, string, string]) => {
    const value = data?.sample?.[key];
    const source = data?.sources?.[key];
    const sourceStatus = source ? data?.components?.[source] : undefined;
    const unavailable = stale || (sourceStatus != null && (sourceStatus.stale || sourceStatus.state !== "ready"));
    const hasValue = value != null && Number.isFinite(value);
    const timestamp = data?.metric_timestamps?.[key];
    return <div key={key} style={{ minWidth: 0 }}>
      <div style={{ color: "var(--lm-text-dim)", fontSize: 12 }}>{title}</div>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "baseline", gap: 6, fontSize: 26, fontWeight: 600, marginTop: 6, color: unavailable ? "var(--lm-text-dim)" : "var(--lm-text)" }}>
        {!unavailable && hasValue ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "N/A"}
        <span style={{ fontSize: 11, fontWeight: 400, color: "var(--lm-text-dim)" }}>{unit}</span>
      </div>
      {source && <div style={{ color: "var(--lm-text-dim)", fontSize: 11, marginTop: 5, overflowWrap: "anywhere" }} title={timestamp != null && Number.isFinite(timestamp) ? new Date(timestamp * 1000).toLocaleString() : undefined}>{source.toUpperCase()}{unavailable || !hasValue ? " · No fresh data" : ""}</div>}
    </div>;
  };
  return (
    <section style={S.card} aria-label="Environmental sensing">
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 16 }}>Environment</h2>
          <div style={{ color: "var(--lm-text-dim)", fontSize: 12, marginTop: 5 }}>Air quality, temperature and humidity</div>
        </div>
        <span role="status" style={{ alignSelf: "flex-start", fontSize: 11, border: "1px solid var(--lm-border)", borderRadius: 20, padding: "4px 9px", color: !available || data?.state === "disabled" ? "var(--lm-text-dim)" : stale ? "var(--lm-amber)" : "var(--lm-green)" }}>{label}</span>
      </div>
      {(error || data?.last_error) && <p role="alert" style={{ color: "var(--lm-amber)" }}>{error || data?.last_error}</p>}
      {!error && componentIssues.length > 0 && <p role="status" style={{ color: "var(--lm-amber)" }}>
        {componentIssues.map(([name, status]) => `${name.toUpperCase()}: ${status.last_error || (status.state === "ready" ? "Stale data" : stateLabels[status.state])}`).join(" · ")}
      </p>}
      {!showMeasurements && <p style={{ color: "var(--lm-text-dim)", fontSize: 13, margin: 0, lineHeight: 1.6 }}>
        {!available ? "Environment sensing is not available on this robot." : data?.state === "disabled" ? "Environment sensors are not enabled on this robot." : error || data?.state === "error" ? "Measurements will appear when the sensor connection recovers." : "Waiting for a measurement."}
      </p>}
      {showMeasurements && <>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(120px, 100%), 1fr))", gap: 22 }}>
          {measurements.slice(0, 4).map(renderMeasurement)}
        </div>
        <details style={{ marginTop: 22, paddingTop: 14, borderTop: "1px solid var(--lm-border)" }}>
          <summary style={{ cursor: "pointer", color: "var(--lm-text-dim)", fontSize: 12 }}>More measurements</summary>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(120px, 100%), 1fr))", gap: 22, marginTop: 18 }}>
            {measurements.slice(4).map(renderMeasurement)}
          </div>
        </details>
      </>}
      {available && (showMeasurements || hasSampleTimestamp) && <p style={{ color: "var(--lm-text-dim)", fontSize: 12, marginTop: 18, lineHeight: 1.6 }}>
        {hasSampleTimestamp ? `Last measurement: ${new Date(sampleTimestamp * 1000).toLocaleString()}` : available ? "Waiting for a measurement." : "No measurements available."}
        {stale && hasSampleTimestamp ? " · No fresh data" : ""}
        {hasGasIndexes ? " · VOC and NOx are indexes, not ppm." : ""}
      </p>}
      {available && data && <div style={{ marginTop: 16 }}><EnvironmentDiagnostics data={data} /></div>}
    </section>
  );
}
