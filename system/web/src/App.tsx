import { useEffect, useState } from "react";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { SourceFooter } from "@/components/SourceFooter";
import Setup from "@/pages/setup";
import { SetupSkeleton } from "@/pages/setup/SetupSkeleton";
import WifiProvision from "@/pages/wifi-provision/WifiProvision";
import Login from "@/pages/Login";
import Monitor from "@/pages/monitor";
import GwConfig from "@/pages/GwConfig";
import { checkInternet, getDeviceConfig, getSetupStatus, safeSearch, scrubLocationSecrets, setApiToken } from "@/lib/api";
import { setLanguage } from "@/lib/i18n";

function isTailscaleHost(host: string): boolean {
  if (host.endsWith(".ts.net")) return true;
  const m = host.match(/^(\d+)\.(\d+)\./);
  if (!m) return false;
  const a = parseInt(m[1], 10);
  const b = parseInt(m[2], 10);
  return a === 100 && b >= 64 && b <= 127;
}

// Setup gate: provisioned -> continue mode, else initial mode; bounces the AP IP to the LAN IP.
function SetupGate() {
  const force = typeof window !== "undefined" && window.location.hash === "#force";
  const [provisioned, setProvisioned] = useState<boolean | null>(force ? false : null);
  useEffect(() => {
    if (force) return;
    let cancelled = false;
    (async () => {
      const status = await getSetupStatus().catch(() => null);
      if (cancelled) return;
      if (status?.set_up_completed === false) { setProvisioned(false); return; }
      const ok = await checkInternet().catch(() => false);
      if (cancelled) return;
      if (!ok) { setProvisioned(false); return; }
      try {
        const s = status ?? await getSetupStatus();
        if (cancelled) return;
        const here = window.location.hostname;
        if (s.lan_ip && s.lan_ip !== here && !isTailscaleHost(here)) {
          // Carry the hash across the origin hop; it is the only record of the deep-linked step.
          window.location.replace(
            `http://${s.lan_ip}${window.location.pathname}${safeSearch()}${window.location.hash}`,
          );
          return;
        }
      } catch { /* keep showing continue mode if status endpoint fails */ }
      if (!cancelled) setProvisioned(true);
    })();
    return () => { cancelled = true; };
  }, [force]);
  // Skeleton, not null: the deep-link hash can only be honored once `mode` resolves.
  if (provisioned === null) return <SetupSkeleton />;
  return <Setup mode={provisioned ? "continue" : "initial"} />;
}

// Probes GET /api/device/config and routes to children, /setup or /login.
function AuthGate({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const [state, setState] = useState<"checking" | "ok" | "login" | "setup">("checking");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await getDeviceConfig();
        if (cancelled) return;
        setLanguage(cfg.stt_language);
        if (!cfg.has_admin_password) {
          setState("setup");
        } else {
          setState("ok");
        }
      } catch (err) {
        if (cancelled) return;
        const status = (err as { status?: number })?.status;
        if (status === 401) setState("login");
        else if (status === 503) setState("setup");
        else setState("ok");
      }
    })();
    return () => { cancelled = true; };
  }, []);
  if (state === "checking") return null;
  if (state === "setup") {
    return <Navigate to={`/setup${safeSearch()}`} replace />;
  }
  if (state === "login") {
    const password = new URLSearchParams(location.search).get("password");
    const next = location.pathname + safeSearch(location.search) + location.hash;
    const params = new URLSearchParams({ next });
    if (password) params.set("password", password);
    return <Navigate to={`/login?${params.toString()}`} replace />;
  }
  return <>{children}</>;
}

// Sends `/` to /monitor, /login or /setup based on auth state.
function RootRedirect() {
  return (
    <AuthGate>
      <Navigate to="/monitor" replace />
    </AuthGate>
  );
}

// Legacy /edit -> /setting redirect that keeps the query string and hash.
function EditRedirect() {
  const location = useLocation();
  return <Navigate to={`/setting${location.search}${location.hash}`} replace />;
}

// Legacy dashboard path -> canonical route, keeping query and hash.
function DashboardRedirect() {
  const location = useLocation();
  return <Navigate to={`/monitor${location.search}${location.hash}`} replace />;
}

// Scrubs secret query params from the URL after they are read.
function useScrubSecrets() {
  useEffect(() => {
    scrubLocationSecrets();
  }, []);
}

// Seeds the Bearer from `llm_api_key` in the URL and exchanges it for a session cookie.
function useBearerFromQuery() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    const token = new URLSearchParams(window.location.search).get("llm_api_key");
    if (!token) return;
    setApiToken(token);
    fetch("/api/login/exchange", { method: "POST" }).catch(() => {
      /* not fatal — Bearer still rides every /api/* call in this tab */
    });
  }, []);
}

function App() {
  // Order matters: read the URL bearer before useScrubSecrets() strips it.
  useBearerFromQuery();
  useScrubSecrets();
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<RootRedirect />} />
        <Route path="/setup" element={<SetupGate />} />
        <Route path="/wifi" element={<WifiProvision />} />
        <Route path="/login" element={<Login />} />
        <Route element={<AuthGate><Monitor /></AuthGate>}>
          <Route path="/monitor" element={null} />
          <Route path="/setting" element={null} />
        </Route>
        <Route path="/edit" element={<EditRedirect />} />
        <Route path="/gw-config" element={<AuthGate><GwConfig /></AuthGate>} />
        <Route path="/dashboard" element={<DashboardRedirect />} />
      </Routes>
      <Toaster richColors position="top-center" />
      <SourceFooter />
    </BrowserRouter>
  );
}

export default App;
