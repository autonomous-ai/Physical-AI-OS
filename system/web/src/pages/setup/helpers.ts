// SetupMode: initial = AP/offline, continue = LAN/online (enrollment available).
export type SetupMode = "initial" | "continue";

// Maps go-playground/validator errors to human-readable field names.
const FIELD_LABELS: Record<string, string> = {
  SSID: "Wi-Fi name",
  Password: "Wi-Fi password",
  LLMAPIKey: "AI Brain API key",
  LLMBaseURL: "AI Brain URL",
  DeviceID: "Device ID",
};

export function normaliseSetupError(message: string): string {
  const matches = [...message.matchAll(/Field validation for '(\w+)' failed on the '(\w+)' tag/g)];
  if (matches.length === 0) return message;
  const missing: string[] = [];
  const other: string[] = [];
  for (const [, field, tag] of matches) {
    const label = FIELD_LABELS[field] ?? field;
    (tag === "required" ? missing : other).push(label);
  }
  const parts: string[] = [];
  if (missing.length > 0) parts.push(`Missing: ${missing.join(", ")}.`);
  if (other.length > 0) parts.push(`Invalid: ${other.join(", ")}.`);
  parts.push("Re-open Setup from the companion app, or add ?debug=true to enter them manually.");
  return parts.join(" ");
}
