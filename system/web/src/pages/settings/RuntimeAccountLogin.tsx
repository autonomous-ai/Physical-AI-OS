import { useEffect, useRef, useState, type ReactNode } from "react";
import { C } from "@/components/setup/shared";
import {
  cancelRuntimeLogin, getRuntimeLogin, startRuntimeLogin, submitRuntimeLoginCode,
  type RuntimeLoginSession, type RuntimeLoginState,
} from "@/lib/api";

const ongoing = (session?: RuntimeLoginSession) => !!session && ["starting", "waiting", "applying"].includes(session.status);
const buttonStyle = { padding: "8px 16px", borderRadius: 7, border: `1px solid ${C.border}`, background: C.surface, color: C.text, fontSize: 12, cursor: "pointer" };

export function RuntimeAccountLogin({ runtime, visible, disabled, onBusyChange, onResume, onConnected, fallback, advanced }: {
  runtime: string;
  visible: boolean;
  onResume: () => void;
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
  onConnected: (historical: boolean) => void;
  fallback: ReactNode;
  advanced: ReactNode;
}) {
  const [state, setState] = useState<RuntimeLoginState>();
  const [provider, setProvider] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [requesting, setRequesting] = useState(false);
  const [reload, setReload] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const observedActive = useRef("");
  const notified = useRef("");
  const onResumeRef = useRef(onResume);
  useEffect(() => { onResumeRef.current = onResume; }, [onResume]);
  const onConnectedRef = useRef(onConnected);
  useEffect(() => { onConnectedRef.current = onConnected; }, [onConnected]);
  const session = state?.session;
  const busy = requesting || ongoing(session);

  useEffect(() => {
    onBusyChange(busy);
    return () => onBusyChange(false);
  }, [busy, onBusyChange]);

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const result = await getRuntimeLogin(abort.signal);
        if (abort.signal.aborted) return;
        if (result.runtime !== runtime) throw new Error("The active runtime changed. Refresh Settings to continue.");
        setState(result);
        setError("");
        if (ongoing(result.session)) {
          observedActive.current = result.session!.id;
          onResumeRef.current();
        }
        // The parent may reconcile a pristine mode baseline from completed history, never a draft.
        if (result.session?.status === "success" && notified.current !== result.session.id) {
          notified.current = result.session.id;
          onConnectedRef.current(observedActive.current !== result.session.id);
        }
        if (ongoing(result.session)) timer = setTimeout(poll, 2000);
      } catch (err) {
        if (!abort.signal.aborted) setError((err as Error).message || "Could not check account connection.");
      }
    };
    void poll();
    return () => { abort.abort(); if (timer) clearTimeout(timer); };
  }, [runtime, reload, visible]);

  async function act(action: (signal: AbortSignal) => Promise<RuntimeLoginSession>) {
    const signal = controller.current?.signal;
    if (!signal || signal.aborted) return;
    setRequesting(true);
    setError("");
    try {
      const result = await action(signal);
      if (signal.aborted) return;
      if (ongoing(result)) observedActive.current = result.id;
      setState((previous) => previous ? { ...previous, session: result } : previous);
      setCode("");
      setReload((value) => value + 1);
    } catch (err) {
      if (!signal.aborted) setError((err as Error).message || "Account connection failed.");
    } finally {
      if (!signal.aborted) setRequesting(false);
    }
  }

  let loginURL: string | undefined;
  try {
    const url = new URL(session?.login_url ?? "");
    if (url.protocol === "https:" && !url.username && !url.password) loginURL = url.href;
  } catch { /* Only valid HTTPS provider URLs become links. */ }
  const chosenProvider = state?.providers.some((item) => item.id === provider) ? provider : state?.providers[0]?.id;

  if (!visible) return null;

  return <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: 12, width: "100%", fontSize: 12, lineHeight: 1.6 }}>
    {error && <div role="alert" style={{ color: C.amber }}>
      {error} <button type="button" style={buttonStyle} disabled={requesting} onClick={() => setReload((value) => value + 1)}>Check again</button>
    </div>}
    {!state ? <p style={{ margin: 0, color: C.textDim }}>Checking sign-in options…</p> : !state.providers.length ? fallback : <>
      <p style={{ margin: 0, color: C.textDim }}>Sign in on your provider’s website. Your current AI connection stays selected until sign-in succeeds; applying the account briefly restarts the runtime.</p>
      {!ongoing(session) && <>
        {state.providers.length > 1 && <fieldset disabled={disabled || requesting} style={{ border: 0, padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 8 }}>
          <legend style={{ marginBottom: 8 }}>Account provider</legend>
          {state.providers.map((item) => <label key={item.id} style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="radio" name="runtime-account-provider" checked={chosenProvider === item.id} onChange={() => setProvider(item.id)} />{item.label}
          </label>)}
        </fieldset>}
        <button type="button" style={buttonStyle} disabled={disabled || requesting || !chosenProvider} onClick={() => void act((signal) => startRuntimeLogin(runtime, chosenProvider!, signal))}>
          {requesting ? "Connecting…" : session?.status === "success" ? "Reconnect account" : "Connect account"}
        </button>
      </>}
      {!busy && advanced}
      {session && <>
        <p role="status" style={{ margin: 0, color: session.status === "error" ? C.amber : C.textDim }}>
          {session.status === "starting" && "Preparing sign-in…"}
          {session.status === "waiting" && "Complete sign-in using the link below. Keep this page open."}
          {session.status === "applying" && "Sign-in complete. Applying your account and restarting the runtime…"}
          {session.status === "success" && "Sign-in completed and account configuration applied."}
          {session.status === "error" && (session.error || "Could not connect your account. Try again.")}
          {session.status === "cancelled" && "Sign-in cancelled. Your previous AI configuration is unchanged."}
        </p>
        {ongoing(session) && loginURL && <a href={loginURL} target="_blank" rel="noopener noreferrer" style={{ color: C.amber }}>Open provider sign-in ↗</a>}
        {ongoing(session) && session.user_code && <div>Enter this code on the provider’s website: <strong style={{ fontFamily: "monospace", userSelect: "all" }}>{session.user_code}</strong></div>}
        {session.status === "waiting" && session.input_required && <>
          <label htmlFor="runtime-login-code">Paste the code or callback URL shown after signing in</label>
          <input id="runtime-login-code" type="text" autoComplete="off" spellCheck={false} value={code} onChange={(event) => setCode(event.target.value)} disabled={requesting} style={{ width: "100%", boxSizing: "border-box", padding: 10, borderRadius: 7, background: C.surface, border: `1px solid ${C.border}`, color: C.text }} />
          <button type="button" style={buttonStyle} disabled={requesting || !code.trim()} onClick={() => void act((signal) => submitRuntimeLoginCode(session.id, code.trim(), signal))}>Complete sign-in</button>
        </>}
        {ongoing(session) && session.status !== "applying" && <button type="button" style={buttonStyle} disabled={requesting} onClick={() => void act((signal) => cancelRuntimeLogin(session.id, signal))}>Cancel sign-in</button>}
      </>}
    </>}
  </div>;
}
