import { useState, useCallback, useEffect, useRef, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { login } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { C, PasswordField } from "@/components/setup/shared";

// Password login; a ?password query pre-fills and auto-submits, then returns to ?next or /monitor.
export default function Login() {
  const [theme, toggleTheme, themeClass] = useTheme();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  useDocumentTitle("Sign in");

  const passwordFromQuery = searchParams.get("password") ?? "";
  const [password, setPassword] = useState(passwordFromQuery);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const autoLoginAttempted = useRef(false);

  // Only same-origin paths are accepted for `next`.
  const nextParam = searchParams.get("next") || "";
  const nextSafe =
    nextParam.startsWith("/") && !nextParam.startsWith("//") ? nextParam : "/monitor";

  const submitPassword = useCallback(async (value: string) => {
    if (!value) return;
    setError(null);
    setBusy(true);
    try {
      await login(value);
      navigate(nextSafe, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }, [nextSafe, navigate]);

  const submit = useCallback((e: FormEvent) => {
    e.preventDefault();
    void submitPassword(password);
  }, [password, submitPassword]);

  useEffect(() => {
    if (!passwordFromQuery || autoLoginAttempted.current) return;
    autoLoginAttempted.current = true;
    void submitPassword(passwordFromQuery);
  }, [passwordFromQuery, submitPassword]);

  return (
    <div className={`lm-root ${themeClass}`} style={{
      minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
      padding: 24,
    }}>
      <div style={{
        width: "100%", maxWidth: 360,
        background: C.card, border: `1px solid ${C.border}`,
        borderRadius: 12, padding: "24px 22px",
        position: "relative",
      }}>
        <button onClick={toggleTheme} style={{
          position: "absolute", top: 12, right: 12,
          background: "none", border: "none", cursor: "pointer",
          fontSize: 14, color: C.textMuted, padding: "4px 6px",
        }} title={`Theme: ${theme}`}>
          {theme === "dark" ? "◑" : "◐"}
        </button>

        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 4, color: C.text }}>
          Sign in
        </div>
        <div style={{ fontSize: 12, color: C.textDim, marginBottom: 18, lineHeight: 1.5 }}>
          Enter the admin password you set during device setup.
          <div style={{ marginTop: 6 }}>
            If you haven't set one, the default is the 4 characters after the
            dash on the sticker at the bottom of your device.
          </div>
        </div>

        {error && (
          <div style={{
            background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
            borderRadius: 8, padding: "9px 12px", fontSize: 12, color: C.red, marginBottom: 14,
          }}>
            {error}
          </div>
        )}

        <form onSubmit={submit}>
          <PasswordField
            label="Admin Password"
            id="login-password"
            value={password}
            onChange={setPassword}
            placeholder="••••••••"
          />
          <button
            type="submit"
            disabled={busy || !password}
            style={{
              width: "100%", padding: "9px 14px", borderRadius: 7,
              fontSize: 13, fontWeight: 600,
              background: busy || !password ? C.surface : C.amber,
              color: busy || !password ? C.textMuted : "#0C0B09",
              border: "none", cursor: busy || !password ? "not-allowed" : "pointer",
              marginTop: 4, opacity: busy ? 0.7 : 1,
            }}
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
