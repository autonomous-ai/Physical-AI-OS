import { HW } from "../types";

export type Measurement = "temperature_c" | "humidity_pct" | "pm1_0_ug_m3" | "pm2_5_ug_m3" | "pm4_0_ug_m3" | "pm10_ug_m3" | "voc_index" | "nox_index" | "co2_ppm";
export interface EnvironmentComponentStatus {
  state: "disabled" | "starting" | "ready" | "error" | "stopped";
  enabled?: boolean;
  bus?: number | null;
  last_error: string | null;
  stale: boolean;
  age_s: number | null;
  timing?: { poll_interval_s: number; retry_interval_s: number; stale_after_s: number; no_data_timeout_s: number };
  sample: ({ timestamp: number | null; device_status?: number } & Partial<Record<Measurement, number | null>>) | null;
}
export interface EnvironmentStatus extends EnvironmentComponentStatus {
  components?: Record<string, EnvironmentComponentStatus>;
  sources?: Partial<Record<Measurement, string>>;
  metric_timestamps?: Partial<Record<Measurement, number | null>>;
}

const states = new Set<EnvironmentStatus["state"]>(["disabled", "starting", "ready", "error", "stopped"]);

export async function fetchEnvironmentStatus(signal: AbortSignal): Promise<EnvironmentStatus> {
  const response = await fetch(`${HW}/environment/status`, { signal });
  if (!response.ok) {
    throw new Error(response.status === 404
      ? "Environment API unavailable on this robot."
      : `Unable to read sensor status (HTTP ${response.status}).`);
  }
  const result: EnvironmentStatus = await response.json();
  if (!states.has(result.state) || typeof result.stale !== "boolean") {
    throw new Error("Invalid sensor status response.");
  }
  return result;
}
