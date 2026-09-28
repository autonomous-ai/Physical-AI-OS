// Separate module so TTSSection.tsx only exports components (Fast Refresh).

// Deepgram is omitted: HAL has no Deepgram TTS backend.
export type ProviderChoice = "autonomous" | "openai" | "elevenlabs" | "piper" | "custom";

// Detect the current choice from persisted (provider, baseUrl).
export function detectChoice(baseUrl: string, provider?: string): ProviderChoice {
  // Piper is URL-less; only the saved provider identifies it.
  if (provider === "piper") return "piper";
  let host = "";
  try { host = new URL(baseUrl).hostname.toLowerCase(); } catch { /* invalid — fall through */ }
  if (!host) return "autonomous";
  if (host.endsWith("autonomous.ai") || host.endsWith("autonomousdev.xyz")) return "autonomous";
  if (host === "api.openai.com") return "openai";
  if (host === "api.elevenlabs.io" || host.endsWith(".elevenlabs.io")) return "elevenlabs";
  return "custom";
}
