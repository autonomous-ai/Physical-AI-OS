import { Brain } from "lucide-react";
import { Field, LockedField, SectionCard } from "./shared";
import { SecretUpdateField } from "@/components/SecretUpdateField";
import type { LlmLoadedState } from "@/hooks/setup/types";

export type LlmMode = "autonomous" | "custom";

export function LLMSection({
  active, llmLoaded,
  llmApiKey, setLlmApiKey,
  llmUrl, setLlmUrl,
  llmModel, setLlmModel,
  mode, onModeChange,
}: {
  active: boolean;
  llmLoaded: LlmLoadedState;
  llmApiKey: string; setLlmApiKey: (v: string) => void;
  llmUrl: string; setLlmUrl: (v: string) => void;
  llmModel: string; setLlmModel: (v: string) => void;
  // Which brain the device is on. Omit both to render the plain editable form
  // (the first-run setup flow, where there is no Autonomous set to fall back to
  // and nothing to choose between yet).
  mode?: LlmMode;
  onModeChange?: (m: LlmMode) => void;
}) {
  const locked = mode === "autonomous";
  return (
    <SectionCard id="llm" title="AI Brain" active={active} icon={<Brain size={17} />}
      description={locked
        ? "Running on the Autonomous brain your robot shipped with. Switch to Custom to use your own provider."
        : "The LLM that powers your robot. Paste the API key and endpoint from your provider."}>

      {mode && onModeChange && (
        <div style={{ marginBottom: 14 }}>
          <label htmlFor="llm_mode" style={{ display: "block", fontSize: 11.5, color: "var(--lm-text-muted, #8a8a8a)", marginBottom: 5 }}>
            Provider
          </label>
          <select
            id="llm_mode"
            value={mode}
            onChange={(e) => onModeChange(e.target.value as LlmMode)}
            style={{
              width: "100%", padding: "9px 10px", borderRadius: 7, fontSize: 12.5,
              background: "var(--lm-surface, #1a1a1a)", color: "var(--lm-text, #eee)",
              border: "1px solid var(--lm-border, #333)",
            }}>
            <option value="autonomous">Autonomous (included)</option>
            <option value="custom">Custom — bring your own</option>
          </select>
        </div>
      )}

      {/* Read-only on Autonomous. These are the credentials the device is sold
          with; letting them be edited in place is how they used to get lost —
          and an operator who wants their own provider is choosing Custom, not
          quietly overwriting this one. The key is never shown either way. */}
      {locked ? (
        <>
          <Field label="API Key" id="llm_api_key" value="•••••••• (included)" onChange={() => {}} readOnly />
          <Field label="Base URL" id="llm_url" value={llmUrl} onChange={() => {}} readOnly />
          <Field label="Model" id="llm_model" value={llmModel} onChange={() => {}} readOnly />
        </>
      ) : (
        <>
          {/* SecretUpdateField handles both empty (Setup) and configured
              (Settings) states inline — a Pencil icon unlocks the input to
              rotate the key without leaving the page. */}
          <SecretUpdateField
            label="API Key"
            id="llm_api_key"
            configured={llmLoaded.apiKey}
            value={llmApiKey}
            onChange={setLlmApiKey}
            placeholder="sk-..."
          />
          <LockedField lockedInitially={llmLoaded.baseUrl} label="Base URL" id="llm_url" value={llmUrl} onChange={setLlmUrl} placeholder="https://api.openai.com/v1" />
          <LockedField lockedInitially={llmLoaded.model} label="Model" id="llm_model" value={llmModel} onChange={setLlmModel} placeholder="gpt-4o-mini" />
        </>
      )}
    </SectionCard>
  );
}
