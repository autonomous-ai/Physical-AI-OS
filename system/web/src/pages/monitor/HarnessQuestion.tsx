import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { harnessRequest } from "./harness-api";

interface Question {
  agentId: string;
  questionRequestId: string;
  focusRevision: string;
  questions: { key: string; q: string; options: string[]; multi: boolean }[];
}
const control: CSSProperties = {
  padding: "8px 12px", borderRadius: 4, border: "1px solid var(--lm-border)",
  background: "var(--lm-surface)", color: "var(--lm-text)", fontSize: 12,
};

export function HarnessQuestion({ connected, disabled, refresh, onAnswer }: {
  connected: boolean; disabled: boolean; refresh: number;
  onAnswer: (body: { questionRequestId: string; focusRevision: string; answers: Record<string, string> }) => Promise<void>;
}) {
  const [question, setQuestion] = useState<Question | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!connected || disabled) return;
    const controller = new AbortController();
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const result = await harnessRequest("/voice-mode/question", { signal: controller.signal, cache: "no-store" });
        if (!disposed) { setQuestion(result?.questionRequestId ? result as Question : null); setError(null); }
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : "Could not read the agent question.");
      } finally { if (!disposed) timer = setTimeout(() => { void poll(); }, 10000); }
    };
    void poll();
    return () => { disposed = true; controller.abort(); if (timer) clearTimeout(timer); };
  }, [connected, disabled, refresh, reload]);
  return <>
    <button type="button" style={control} disabled={!connected || disabled} onClick={() => setReload(value => value + 1)}>Refresh agent question</button>
    {question && <QuestionForm key={`${question.agentId}:${question.questionRequestId}:${question.focusRevision}`} question={question}
      disabled={disabled || !connected || Boolean(error)} onAnswer={answers => onAnswer({ questionRequestId: question.questionRequestId, focusRevision: question.focusRevision, answers })} />}
    {error && <p role="alert" style={{ margin: 0, color: "var(--lm-red)" }}>{error}</p>}
  </>;
}

function QuestionForm({ question, disabled, onAnswer }: {
  question: Question; disabled: boolean; onAnswer: (answers: Record<string, string>) => Promise<void>;
}) {
  const [selections, setSelections] = useState<Record<string, string[]>>({});
  const [custom, setCustom] = useState<Record<string, string>>({});
  const answers = Object.fromEntries(question.questions.map(item => [item.key,
    custom[item.key]?.trim() || (selections[item.key] ?? []).join(", "),
  ]));
  const complete = question.questions.length > 0 && question.questions.every(item => Boolean(answers[item.key]));
  return <form onSubmit={event => { event.preventDefault(); if (!disabled && complete) void onAnswer(answers); }}
    style={{ display: "flex", flexDirection: "column", gap: 10 }}>
    <strong>Agent needs your answer</strong>
    <span>Answer here or speak your response to the robot.</span>
    {question.questions.map(item => <fieldset key={item.key} disabled={disabled}
      style={{ margin: 0, padding: 10, border: "1px solid var(--lm-border)", display: "flex", flexDirection: "column", gap: 8 }}>
      <legend>{item.q || item.key}{item.multi ? " (select all that apply)" : ""}</legend>
      {item.options.map((option, index) => <label key={`${index}:${option}`} style={{ display: "flex", gap: 8, alignItems: "start" }}>
        <input type={item.multi ? "checkbox" : "radio"} name={item.key}
          checked={!custom[item.key] && (selections[item.key] ?? []).includes(option)}
          onChange={event => {
            const checked = event.target.checked;
            setCustom(previous => ({ ...previous, [item.key]: "" }));
            setSelections(previous => ({ ...previous, [item.key]: item.multi
              ? checked ? [...(previous[item.key] ?? []), option] : (previous[item.key] ?? []).filter(value => value !== option)
              : [option] }));
          }} />
        {option}
      </label>)}
      <label style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {item.options.length ? "Or type your own answer" : "Your answer"}
        <input type="text" style={control} value={custom[item.key] ?? ""} onChange={event => {
          const value = event.target.value;
          setCustom(previous => ({ ...previous, [item.key]: value }));
          setSelections(previous => ({ ...previous, [item.key]: [] }));
        }} />
      </label>
    </fieldset>)}
    <button type="submit" style={control} disabled={disabled || !complete}>Send answer</button>
  </form>;
}
