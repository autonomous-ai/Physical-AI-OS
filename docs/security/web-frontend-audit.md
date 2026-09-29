# Security Audit: Lamp Web Frontend

Date: 2026-05-16  
Repo: `lamp`  
Scope: Lamp frontend only (`system/web/src`, `system/web/package.json`, browser-facing behavior).  
Path note (2026-09-28): file paths below were updated to the current `autonomous-os` layout (`lamp/web/` → `system/web/`, `components/edit/*` → `pages/settings/*`, `system/server/openclaw/delivery/sse/` → `system/server/agent/delivery/http/`). `pages/EditConfig.tsx` has since been removed (settings now live under `system/web/src/pages/settings/`); `lib/hardwareApi.ts`, `lib/http.ts` and `lib/safeSearch.ts` are proposed files, not existing ones.  
Instruction: report issues and remediation guidance only; do **not** patch runtime code in this document.

## Executive summary

The Lamp web app is a privileged device-control UI, not a normal public website. It can:

- Read and write device config, including API keys, bot tokens, WiFi password, MQTT password.
- Call raw HAL hardware endpoints under `/hw/*` for camera, mic/speaker, servo, face, bluetooth, display, voice, LED.
- Open an interactive shell through `/api/system/shell`.
- Fetch raw OpenClaw config and gateway token from `/api/agent/config-json`.
- Generate `/gw/chat#token=...` links containing the gateway token.
- Store chat/history/UI data in `localStorage`.
- Accept secrets through URL query parameters during setup.

The biggest frontend-specific risks are:

1. **The UI assumes backend APIs are safe to call from any page load.** There is no client-side auth/session model, and many privileged controls are visible/callable directly.
2. **Secrets are fetched into browser state** (`EditConfig`, setup prefill, `GwConfig`) and sometimes displayed in full.
3. **Secrets are accepted through URL query strings**, which can leak through browser history, logs, referrers, screenshots, copy/paste, and redirects.
4. **OpenClaw gateway token is fetched from config and placed into URL fragment.** Fragments are safer than query strings for HTTP logs, but still visible to browser extensions, screenshots, copied URLs, and any script running on the page.
5. **Raw `/hw/*` and shell controls are directly exposed in UI.** Once backend blocks them, parts of the UI will break unless redesigned through authenticated/sanitized endpoints.
6. **No Content Security Policy / clickjacking policy is visible at the web layer.** If an XSS or injected script occurs, it can operate the full device UI.

Important note: frontend changes are not sufficient alone. Security must be enforced server-side. The frontend should be updated so it does not depend on unsafe backend behavior and does not unnecessarily pull/store/display secrets.

---

## Current frontend trust model

### API base

`system/web/src/lib/api.ts` uses relative API paths by default:

```ts
const API_BASE =
  import.meta.env.VITE_API_BASE ??
  import.meta.env.VITE_NETWORK_API ??
  import.meta.env.VITE_API_URL ??
  "";
```

Most calls go to:

- `/api/*` for Lamp Go server
- `/hw/*` for HAL hardware server via nginx
- `/gw/*` for OpenClaw gateway via nginx

### No frontend auth wrapper

`apiRequest` simply calls `fetch` and does not attach an auth token/header:

```ts
async function apiRequest<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options);
  const json = (await res.json()) as JSONResponse<T>;
  ...
}
```

This means any future backend auth must be wired into `apiRequest`, hardware fetch wrappers, SSE, and WebSocket creation.

---

## Finding 1 — Frontend fetches full device config including secrets

### Severity

**Critical** when `/api/device/config` is reachable from LAN or unauthenticated UI.

### Evidence

`system/web/src/lib/api.ts` defines `DeviceConfig` with raw secrets:

```ts
export interface DeviceConfig {
  telegram_bot_token: string;
  slack_bot_token: string;
  slack_app_token: string;
  discord_bot_token: string;
  llm_api_key: string;
  deepgram_api_key: string;
  stt_api_key: string;
  tts_api_key: string;
  network_password: string;
  mqtt_password: string;
  ...
}
```

`getDeviceConfig()` fetches it:

```ts
export async function getDeviceConfig(): Promise<DeviceConfig> {
  return apiRequest<DeviceConfig>(`${API_BASE}/api/device/config`);
}
```

`system/web/src/pages/EditConfig.tsx` loads secrets directly into React state:

```ts
setPassword(cfg.network_password ?? "");
setLlmApiKey(cfg.llm_api_key ?? "");
setDeepgramApiKey(cfg.deepgram_api_key ?? "");
setSttApiKey(cfg.stt_api_key ?? "");
setTtsApiKey(cfg.tts_api_key ?? "");
setTeleToken(cfg.telegram_bot_token ?? "");
setSlackBotToken(cfg.slack_bot_token ?? "");
setSlackAppToken(cfg.slack_app_token ?? "");
setDiscordBotToken(cfg.discord_bot_token ?? "");
setMqttPassword(cfg.mqtt_password ?? "");
```

`system/web/src/hooks/setup/useConfigPrefill.ts` also pre-fills secrets from config.

### Why it is risky

Once secrets are in browser state, they can be exposed through:

- Browser devtools.
- Browser extensions.
- XSS, if any injection appears later.
- Screenshots/screen sharing.
- Shoulder surfing.
- Password reveal controls.
- Memory dumps or crash reports.

Also, this design forces the backend to return raw secrets to render the edit page. That makes `/api/device/config` a high-value endpoint.

### Required remediation

Do not fetch raw secrets to render config screens.

#### Backend contract change needed

Replace `GET /api/device/config` with a redacted/sanitized response:

```ts
export interface DeviceConfigPublic {
  channel: string;
  telegram_configured: boolean;
  slack_configured: boolean;
  discord_configured: boolean;
  llm_configured: boolean;
  deepgram_configured: boolean;
  stt_configured: boolean;
  tts_configured: boolean;
  network_ssid: string;
  network_configured: boolean;
  mqtt_configured: boolean;
  mqtt_endpoint: string;
  mqtt_port: number;
  device_id: string;
  mac: string;
  llm_model: string;
  llm_base_url?: string; // optional; consider redacting host if sensitive
  stt_language: string;
  tts_provider: string;
  tts_voice: string;
}
```

#### Frontend file: `system/web/src/lib/api.ts`

Replace raw `DeviceConfig` for GET with a sanitized type. Keep a separate write type:

```ts
export interface DeviceConfigPublic { ... }
export interface DeviceConfigUpdate {
  ssid?: string;
  password?: string;
  llm_api_key?: string;
  telegram_bot_token?: string;
  // write-only fields; never returned by GET
}
```

Change:

```ts
export async function getDeviceConfig(): Promise<DeviceConfig>
```

To:

```ts
export async function getDeviceConfig(): Promise<DeviceConfigPublic>
```

Change:

```ts
export async function updateDeviceConfig(body: Partial<DeviceConfig> & { password?: string; ssid?: string })
```

To:

```ts
export async function updateDeviceConfig(body: DeviceConfigUpdate)
```

#### Frontend file: `system/web/src/pages/EditConfig.tsx`

Do not set secret state from GET response. Instead:

- Initialize secret input values as empty strings.
- Show “Configured” badges based on `*_configured` booleans.
- Let user enter a replacement value only when they choose to update.
- Do not send empty strings unless the user explicitly clicked “clear”.

Example state model:

```ts
const [llmConfigured, setLlmConfigured] = useState(false);
const [llmApiKey, setLlmApiKey] = useState(""); // blank means unchanged
const [clearLlmApiKey, setClearLlmApiKey] = useState(false);
```

Update payload rules:

```ts
const payload: DeviceConfigUpdate = {};
if (llmApiKey.trim()) payload.llm_api_key = llmApiKey.trim();
if (clearLlmApiKey) payload.llm_api_key = "";
```

### Acceptance checks

In browser devtools Network tab, `GET /api/device/config` response must not contain:

- `telegram_bot_token`
- `slack_bot_token`
- `slack_app_token`
- `discord_bot_token`
- `llm_api_key`
- `deepgram_api_key`
- `stt_api_key`
- `tts_api_key`
- `network_password`
- `mqtt_password`

Run:

```sh
curl -s http://<device-ip>/api/device/config | grep -Ei 'token|api_key|password|secret'
```

Expected: no raw secrets.

---

## Finding 2 — Edit-mode token fields intentionally reveal saved tokens

### Severity

**High**.

### Evidence

`system/web/src/pages/settings/ChannelSection.tsx` comment:

```ts
// Edit-mode channel credentials use LockedField for tokens (not LockedPasswordField
// like Setup) — operator wants to see and verify saved tokens at a glance.
```

Fields:

```tsx
<LockedField lockedInitially={channelLoaded.teleToken} label="Bot Token" ... value={teleToken} />
<LockedField lockedInitially={channelLoaded.slackBotToken} label="Bot Token" ... value={slackBotToken} />
<LockedField lockedInitially={channelLoaded.slackAppToken} label="App Token" ... value={slackAppToken} />
<LockedField lockedInitially={channelLoaded.discordBotToken} label="Bot Token" ... value={discordBotToken} />
```

### Why it is risky

Full bot tokens are displayed in regular text fields. This increases accidental leakage via:

- Screenshots.
- Screen sharing.
- Shoulder surfing.
- Browser autofill/inspection.
- Any script or extension reading DOM/input values.

### Required remediation

Use write-only secret UX:

- Do not show saved token values.
- Show status: “Configured” / “Not configured”.
- Provide buttons:
  - “Replace token” opens an empty password field.
  - “Clear token” explicit destructive action.
  - Optional “Test connection” without revealing token.

#### File: `system/web/src/pages/settings/ChannelSection.tsx`

Replace `LockedField` for token fields with a component like:

```tsx
<SecretUpdateField
  configured={channelLoaded.teleToken}
  label="Telegram Bot Token"
  value={teleToken}
  onChange={setTeleToken}
  onClear={() => setClearTeleToken(true)}
/>
```

`SecretUpdateField` behavior:

- If configured and not editing: display `Configured •••••`.
- If user clicks replace: empty password input.
- Never prefill with actual token.

### Acceptance checks

After loading Edit Config page:

- DOM should not contain raw token values.
- Inputs for configured secrets should be empty until user types replacement.
- Browser devtools React state should not contain raw server-returned tokens.

---

## Finding 3 — Setup accepts secrets through URL query parameters

### Severity

**High**.

### Evidence

`system/web/src/hooks/setup/useSetupUrlParams.ts` reads secrets from query string:

```ts
teleToken: searchParams.get("tele_token") ?? "",
slackBotToken: searchParams.get("slack_bot_token") ?? "",
slackAppToken: searchParams.get("slack_app_token") ?? "",
discordBotToken: searchParams.get("discord_bot_token") ?? "",
llmApiKey: searchParams.get("llm_api_key") ?? "",
deepgramApiKey: searchParams.get("deepgram_api_key") ?? "",
ttsApiKey: searchParams.get("tts_api_key") ?? "",
mqttPassword: searchParams.get("mqtt_password") ?? "",
```

`Setup.tsx` sends these into setup payload:

```ts
telegram_bot_token: urlParams.teleToken || teleToken,
llm_api_key: urlParams.llmApiKey || llmApiKey,
deepgram_api_key: urlParams.deepgramApiKey || undefined,
tts_api_key: ttsApiKey || undefined,
mqtt_password: mqttPassword || urlParams.mqttPassword || undefined,
```

Redirect logic preserves `window.location.search`:

- `App.tsx`:
  ```ts
  window.location.replace(`http://${s.lan_ip}${window.location.pathname}${window.location.search}`);
  ```

- `Setup.tsx` / setup status hooks also preserve query params in redirects.

### Why it is risky

Secrets in URL query strings leak through many channels:

- Browser history.
- Reverse proxy logs.
- Server access logs.
- Screenshots and copied links.
- Referrer headers to other origins.
- Analytics/error reporting if added later.
- Redirects that preserve `window.location.search`.

This is especially dangerous for LLM API keys and bot tokens.

### Required remediation

Do not pass secrets in URL query parameters.

#### Preferred design

Use a short-lived setup code or one-time prefill ID:

```text
/setup?prefill_id=abc123
```

Then browser POSTs the setup code to backend over local connection:

```ts
POST /api/device/setup-prefill/consume
{ "prefill_id": "abc123" }
```

Backend returns non-secret metadata or writes secrets server-side without exposing them to the browser.

#### Minimal interim mitigation

If query params must be supported temporarily:

1. Read them once.
2. Move them into in-memory state.
3. Immediately scrub URL using `history.replaceState`.
4. Do not preserve query params during redirects.

File: `system/web/src/hooks/setup/useSetupUrlParams.ts` or `Setup.tsx`:

```ts
useEffect(() => {
  const secretKeys = [
    "tele_token",
    "slack_bot_token",
    "slack_app_token",
    "discord_bot_token",
    "llm_api_key",
    "deepgram_api_key",
    "tts_api_key",
    "mqtt_password",
  ];
  const params = new URLSearchParams(window.location.search);
  const hadSecret = secretKeys.some((k) => params.has(k));
  if (hadSecret) {
    for (const k of secretKeys) params.delete(k);
    const clean = `${window.location.pathname}${params.toString() ? `?${params}` : ""}${window.location.hash}`;
    window.history.replaceState(null, "", clean);
  }
}, []);
```

But this only reduces browser history/referrer exposure after page load; it does not prevent initial proxy/server logs.

#### Redirect fix

Do not preserve secrets in redirects. Replace:

```ts
${window.location.pathname}${window.location.search}
```

With a sanitized path:

```ts
const safeSearch = buildSafeSearch(window.location.search);
`${window.location.pathname}${safeSearch}`
```

Where `buildSafeSearch` removes all secret keys.

### Acceptance checks

Open setup with:

```text
http://lamp.local/setup?llm_api_key=SECRET&tele_token=SECRET
```

Expected after load:

- Address bar no longer contains secret params.
- Redirects do not carry secret params.
- Server-side design eventually avoids receiving secrets in URL at all.

---

## Finding 4 — Frontend fetches OpenClaw config and extracts gateway token

### Severity

**High**.

### Evidence

`system/web/src/pages/monitor/index.tsx`:

```ts
fetch("/api/agent/config-json")
  .then((r) => r.json())
  .then((res) => {
    const t = res?.data?.gateway?.auth?.token;
    if (typeof t === "string" && t) setGwToken(t);
  })
```

Then constructs gateway link:

```ts
const gwHref = `/gw/chat?session=agent:main:main${gwToken ? `#token=${gwToken}` : ""}`;
```

`system/web/src/pages/GwConfig.tsx` fetches the same raw config and renders it:

```ts
fetch(`${API}/agent/config-json`)
...
setRaw(JSON.stringify(res.data, null, 2));
...
<pre>{raw}</pre>
```

`system/web/src/pages/monitor/ChatSection.tsx` also fetches `config-json`.

### Why it is risky

The gateway token becomes browser state and part of a URL fragment. URL fragments are not sent to the server in HTTP requests, but they are still exposed to:

- The browser address bar.
- Browser history/session restore.
- Screenshots/screen sharing.
- Extensions.
- Any same-origin script.
- Copy/paste of the URL.

Rendering raw `openclaw.json` can expose multiple secrets beyond the gateway token.

### Required remediation

Do not expose raw OpenClaw config to the frontend.

#### Recommended backend change

Create a server-side endpoint that opens/redirects to gateway without returning token to the browser, or a redacted summary endpoint.

Options:

1. **Local-only gateway UI**: remove remote web link entirely; show “Gateway is local-only”.
2. **Backend-minted short-lived gateway session**:
   - Browser calls authenticated `/api/agent/gateway-session`.
   - Backend returns short-lived one-time URL/token with narrow scope.
   - Token expires quickly and is not the raw gateway auth token.
3. **Redacted config summary**:
   - Replace `config-json` UI with `/api/agent/config-summary` that redacts all secrets.

#### Frontend changes

- `system/web/src/pages/monitor/index.tsx`: remove fetch of `/api/agent/config-json` and do not build `#token=` with raw token.
- `system/web/src/pages/GwConfig.tsx`: call `config-summary` or delete page from production.
- `system/web/src/pages/monitor/ChatSection.tsx`: stop fetching raw config; use sanitized status/model endpoint.

### Acceptance checks

Search built assets/source:

```sh
grep -R "config-json\|#token=" system/web/src
```

Expected after fix:

- No production path fetches raw config.
- No raw gateway token is placed into URLs.

---

## Finding 5 — Frontend directly calls raw `/hw/*` hardware endpoints

### Severity

**High**.

### Evidence

Many files call `/hw/*` directly:

- `system/web/src/pages/monitor/OverviewSection.tsx`
  - `/hw/audio/volume`
  - `/hw/voice/mute` / `/hw/voice/unmute`
  - `/hw/speaker/mute` / `/hw/speaker/unmute`
  - `/hw/emotion`
  - `/hw/servo/play`
  - `/hw/servo/release`

- `system/web/src/pages/monitor/CameraSection.tsx`
  - `/hw/camera/*`
  - `/hw/servo/track*`

- `system/web/src/pages/monitor/ServoSection.tsx`
  - `/hw/servo/*`

- `system/web/src/pages/monitor/BluetoothSection.tsx`
  - `/hw/bluetooth/*`

- `system/web/src/pages/monitor/FaceOwnersSection.tsx`
  - `/hw/face/*`
  - `/hw/voice/strangers/*`

- `system/web/src/pages/settings/FaceSection.tsx`
  - `/hw/face/*`

- `system/web/src/pages/settings/VoiceSection.tsx`
  - `/hw/speaker/*`
  - `/hw/face/file/*`

- `system/web/src/lib/api.ts`
  - `/hw/voice/speak`

- Monitor embeds:
  ```tsx
  <iframe title="API Docs" src="/hw/docs" />
  ```

### Why it is risky

The frontend normalizes external browser access to raw hardware APIs. That conflicts with the desired security boundary: only same-machine Go/OpenClaw should call HAL.

If backend/nginx hardening denies external `/hw/*`, the current UI will break. If `/hw/*` remains open for UI compatibility, hardware stays exposed.

### Required remediation

Do not have the browser call raw HAL endpoints.

Create Go server endpoints that:

- Require admin auth.
- Validate input.
- Apply rate limits.
- Call HAL locally (`http://127.0.0.1:5001`) server-side.
- Return sanitized responses.

Example replacements:

| Current frontend call | Replace with |
|---|---|
| `/hw/health` | `/api/hardware/health` |
| `/hw/camera/snapshot` | `/api/hardware/camera/snapshot` with auth and explicit permission |
| `/hw/camera/stream` | `/api/hardware/camera/stream` with auth or disabled remotely |
| `/hw/servo/play` | `/api/hardware/servo/play` |
| `/hw/servo/release` | `/api/hardware/servo/release` |
| `/hw/voice/speak` | `/api/hardware/voice/speak` |
| `/hw/bluetooth/*` | `/api/hardware/bluetooth/*` |
| `/hw/docs` | remove from production or local-only dev link |

#### Frontend refactor

Create a hardware API wrapper:

```ts
// system/web/src/lib/hardwareApi.ts
import { apiRequest } from "./api";

export function getHardwareHealth() {
  return apiRequest<HardwareHealth>("/api/hardware/health");
}

export function playServoAnimation(name: string) {
  return apiRequest("/api/hardware/servo/play", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}
```

Then replace all direct `/hw/*` fetches.

### Acceptance checks

After refactor:

```sh
grep -R "fetch(.*\/hw\|src=\"/hw\|href=\"/hw" system/web/src
```

Expected:

- No production browser calls to `/hw/*`.
- Optional dev-only links are gated behind local/dev flag.

---

## Finding 6 — Web UI exposes interactive shell

### Severity

**Critical**.

### Evidence

`system/web/src/pages/monitor/CliSection.tsx` opens a WebSocket:

```ts
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(`${proto}//${location.host}/api/system/shell`);
```

It sends terminal input directly:

```ts
const dataDisposable = term.onData((data) => {
  if (ws.readyState === WebSocket.OPEN) ws.send(data);
});
```

Monitor renders CLI section:

```tsx
{section === "cli" && <CliSection />}
```

### Why it is risky

This is browser-accessible shell access. If `/api/system/shell` is not strongly protected server-side, any user with web access can get a shell.

Even with auth, exposing shell in the web UI increases accidental misuse risk.

### Required remediation

Remove CLI from production UI by default.

#### File: `system/web/src/pages/monitor/index.tsx`

Gate CLI nav and render by build flag:

```ts
const ENABLE_WEB_CLI = import.meta.env.DEV && import.meta.env.VITE_ENABLE_WEB_CLI === "true";
```

Only include CLI when enabled:

```tsx
{ENABLE_WEB_CLI && section === "cli" && <CliSection />}
```

Also remove/hide CLI nav item where `NAV` is defined.

#### File: `system/web/src/pages/monitor/CliSection.tsx`

If retained:

- Require explicit “I understand” prompt.
- Display local-only status.
- Attach admin auth token if backend requires it.
- Do not auto-open shell on component mount; require button click.

### Acceptance checks

Production build:

```sh
grep -R "api/system/shell\|CliSection" system/web/dist
```

Expected:

- No shell UI in production unless explicitly enabled.

---

## Finding 7 — Chat history and flow data stored in `localStorage`

### Severity

**Medium**.

### Evidence

`system/web/src/pages/monitor/ChatSection.tsx` stores conversation data:

```ts
const raw = localStorage.getItem(CONVOS_KEY);
...
localStorage.setItem(CONVOS_KEY, JSON.stringify(trimmed));
```

`FlowSection` stores UI filters:

```ts
localStorage.setItem("lamp-excluded-types-v1", JSON.stringify([...next]));
```

`LogsSection` stores log UI state.

### Why it is risky

Chat history may include sensitive text, tool output summaries, filenames, people names, or internal operational data. `localStorage` persists indefinitely and is readable by any same-origin script or browser extension.

Current code strips large image data URLs, which is good, but text content still persists.

### Required remediation

1. Make local chat persistence opt-in or time-limited.
2. Provide “Clear local data” button in UI.
3. Store only non-sensitive UI preferences by default.
4. If persistence is needed, use IndexedDB/sessionStorage with TTL and clear-on-logout.
5. Never store API keys/tokens in localStorage.

#### File: `system/web/src/pages/monitor/ChatSection.tsx`

Add TTL wrapper:

```ts
const MAX_HISTORY_AGE_MS = 24 * 60 * 60 * 1000;
```

Store:

```ts
localStorage.setItem(CONVOS_KEY, JSON.stringify({ savedAt: Date.now(), conversations: trimmed }));
```

Load:

```ts
if (Date.now() - parsed.savedAt > MAX_HISTORY_AGE_MS) {
  localStorage.removeItem(CONVOS_KEY);
  return [];
}
```

Add UI button:

```tsx
<button onClick={clearLocalHistory}>Clear local chat history</button>
```

### Acceptance checks

- Chat history expires after TTL.
- Clear button removes localStorage keys.
- No secrets are stored in localStorage.

---

## Finding 8 — External links and opened hardware files need safer handling

### Severity

**Medium**.

### Evidence

Good: chat links use:

```tsx
target="_blank" rel="noopener noreferrer"
```

Some places use only `rel="noreferrer"`, which usually implies no opener in modern browsers but is less explicit:

```tsx
<a href={url} target="_blank" rel="noreferrer">
```

Some code uses `window.open`:

```tsx
onClick={() => window.open(`/hw/face/photo/${p.label}/${photo}`, "_blank")}
```

### Why it is risky

Without explicit `noopener`, a newly opened page can potentially access `window.opener` in older/browser-specific cases. Also, URLs built from labels/filenames must be encoded to avoid path confusion.

### Required remediation

1. Use `rel="noopener noreferrer"` consistently.
2. For `window.open`, use:

```ts
const w = window.open(url, "_blank", "noopener,noreferrer");
if (w) w.opener = null;
```

3. Encode path segments:

```ts
const url = `/hw/face/photo/${encodeURIComponent(p.label)}/${encodeURIComponent(photo)}`;
```

### Acceptance checks

```sh
grep -R "target=\"_blank\"" system/web/src
```

Every result should include `rel="noopener noreferrer"` or equivalent.

```sh
grep -R "window.open" system/web/src
```

Every result should use `noopener,noreferrer` and encoded path segments.

---

## Finding 9 — No visible Content Security Policy / anti-clickjacking policy

### Severity

**Medium to High**.

### Evidence

No CSP or frame policy was found in frontend source. The web app is served by nginx from setup scripts; security headers should be set there.

### Why it is risky

The web app is a privileged device control panel. If it can be framed by another site, clickjacking can trick the user into clicking dangerous controls. If an injection bug appears later, a weak CSP allows arbitrary script execution, which can call all same-origin APIs.

### Required remediation

Set security headers in nginx for the web app.

#### File: `scripts/provision/setup.sh` nginx config

Add to `server { ... }`:

```nginx
add_header X-Frame-Options "DENY" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "no-referrer" always;
add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'" always;
```

Notes:

- `style-src 'unsafe-inline'` may be needed because the React app uses inline styles. Avoid `script-src 'unsafe-inline'`.
- If charts/assets require blob/data, keep the minimal needed directives.
- If `/gw-config` iframe remains same-origin, `frame-src 'self'` allows it. `frame-ancestors 'none'` prevents external sites from framing Lamp.

Mirror in `scripts/imager/build.sh`.

### Acceptance checks

```sh
curl -I http://<device-ip>/monitor
```

Expected headers:

- `Content-Security-Policy`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`

---

## Finding 10 — No central authenticated fetch client

### Severity

**Medium** now; **High** once backend auth is added.

### Evidence

Only `apiRequest` wraps some `/api` calls. Many components call `fetch` directly:

- `/hw/*`
- `/api/agent/*`
- `/api/logs/*`
- `/api/sensing/event`
- `/api/system/software-update/*`
- EventSource streams
- WebSocket shell

### Why it is risky

When backend auth is added, direct fetch calls will miss auth headers, inconsistent error handling, CSRF protections, and 401 handling.

### Required remediation

Centralize browser API access.

Create:

```ts
// system/web/src/lib/http.ts
export async function apiFetch(input: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  const token = getAdminToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(input, { ...init, headers, credentials: "same-origin" });
  if (res.status === 401) handleUnauthorized();
  return res;
}
```

For SSE:

- Native `EventSource` cannot set custom headers.
- Prefer cookie/session auth for SSE, or use a fetch-based SSE polyfill that supports headers.
- If using bearer tokens, avoid putting tokens in query strings.

For WebSocket:

- Browser WebSocket cannot set custom headers.
- Use cookie/session auth or a short-lived one-time ticket in query string.
- Do not put long-lived admin/gateway tokens in query string.

### Acceptance checks

```sh
grep -R "fetch(" system/web/src
```

Expected after refactor:

- Most calls go through `apiFetch` or typed wrappers.
- Exceptions are documented.

---

## Finding 11 — Setup redirect preserves query string containing possible secrets

### Severity

**High** because it amplifies Finding 3.

### Evidence

`system/web/src/App.tsx`:

```ts
window.location.replace(`http://${s.lan_ip}${window.location.pathname}${window.location.search}`);
```

`system/web/src/hooks/setup/useSetupStatusPolling.ts` has similar redirect construction preserving `window.location.search`.

`Setup.tsx` mDNS link also preserves query params:

```tsx
href={`http://${lampMdnsHost}.local${window.location.pathname}${window.location.search}`}
```

### Why it is risky

If the URL contains `llm_api_key`, `tele_token`, etc., redirects propagate those secrets to:

- New hostnames/IPs.
- Browser history entries.
- Access logs on each host/proxy.
- Copyable mDNS links.

### Required remediation

Define a single sanitizer:

```ts
const SECRET_QUERY_KEYS = new Set([
  "tele_token",
  "slack_bot_token",
  "slack_app_token",
  "discord_bot_token",
  "llm_api_key",
  "deepgram_api_key",
  "stt_api_key",
  "tts_api_key",
  "mqtt_password",
]);

export function safeSearch(search: string): string {
  const p = new URLSearchParams(search);
  for (const k of SECRET_QUERY_KEYS) p.delete(k);
  const s = p.toString();
  return s ? `?${s}` : "";
}
```

Use it in:

- `system/web/src/App.tsx`
- `system/web/src/hooks/setup/useSetupStatusPolling.ts`
- `system/web/src/pages/setup/Setup.tsx`

Replace every redirect/link preserving `window.location.search` with sanitized search.

### Acceptance checks

Open:

```text
/setup?llm_api_key=SECRET&device_id=lamp1
```

After redirect, URL must not contain `llm_api_key`.

---

## Finding 12 — API docs iframe exposes raw hardware Swagger UI

### Severity

**Medium to High**.

### Evidence

`system/web/src/pages/monitor/index.tsx`:

```tsx
{section === "api-docs" && (
  <iframe
    title="API Docs"
    src="/hw/docs"
    style={iframeStyle}
  />
)}
```

### Why it is risky

Swagger UI exposes full raw hardware API documentation and lets users execute requests. This normalizes raw `/hw/*` access from browser and conflicts with local-only hardware boundary.

### Required remediation

Remove API docs iframe from production.

Options:

1. Dev-only:

```ts
const SHOW_HW_API_DOCS = import.meta.env.DEV && import.meta.env.VITE_SHOW_HW_API_DOCS === "true";
```

2. Local-only notice:

Show message:

```tsx
Hardware API docs are local-only. SSH to device and open http://127.0.0.1:5001/docs.
```

3. Replace with sanitized documentation page describing safe `/api/hardware/*` endpoints.

### Acceptance checks

Production build should not contain `/hw/docs` iframe:

```sh
grep -R 'src="/hw/docs"\|/hw/docs' system/web/dist
```

Expected: no production references.

---

## Finding 13 — Secrets may be sent to `/hw/voice/speak` for TTS preview

### Severity

**Medium to High**.

### Evidence

`system/web/src/lib/api.ts`:

```ts
export async function testTTSVoice(voice: string, opts: TestTTSOptions = {}): Promise<void> {
  const apiKey = (opts.ttsApiKey && opts.ttsApiKey.trim()) || opts.llmApiKey || "";
  const baseUrl = (opts.ttsBaseUrl && opts.ttsBaseUrl.trim()) || opts.llmBaseUrl || "";
  await fetch("/hw/voice/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text: opts.text || demoPhraseFor(opts.lang),
      voice,
      provider: opts.provider || undefined,
      tts_api_key: apiKey || undefined,
      tts_base_url: baseUrl || undefined,
    }),
  });
}
```

### Why it is risky

This sends API keys from browser directly to the hardware API. If `/hw/*` is exposed or logged, secrets can leak. It also encourages frontend-to-hardware secret passing.

### Required remediation

Move TTS preview to a Go backend endpoint:

```text
POST /api/voice/preview
```

Backend should:

- Require admin auth.
- Use stored credentials server-side or a short-lived submitted key only for preview.
- Avoid logging request body.
- Call HAL locally.

Frontend should not call `/hw/voice/speak` directly.

### Acceptance checks

```sh
grep -R "tts_api_key\|/hw/voice/speak" system/web/src
```

Expected:

- No direct hardware call with API key in frontend.

---

## Finding 14 — Camera/face/voice file URLs are built into DOM and opened directly

### Severity

**Medium**.

### Evidence

Examples:

```tsx
src={`/hw/face/photo/${p.label}/${photo}`}
onClick={() => window.open(`/hw/face/photo/${p.label}/${photo}`, "_blank")}
```

```ts
const url = `/hw/face/file/${p.label}/voice/${encodeURIComponent(file)}`;
```

### Why it is risky

- Raw file URLs expose private photos/audio through `/hw/*`.
- Some path segments are not consistently `encodeURIComponent`-encoded.
- Direct links bypass any future API wrapper/auth unless backend enforces it.

### Required remediation

1. Serve private media through authenticated `/api/media/*` endpoints.
2. Encode all path segments.
3. Prefer opaque IDs over filesystem-like names.
4. Add `Content-Disposition` and content-type controls server-side.

Frontend replacement:

```ts
const url = `/api/media/face-photo/${encodeURIComponent(ownerId)}/${encodeURIComponent(photoId)}`;
```

### Acceptance checks

```sh
grep -R "/hw/face/photo\|/hw/face/file" system/web/src
```

Expected: no production raw `/hw` media URLs.

---

## Recommended frontend refactor plan

### Phase 1 — Stop pulling and displaying raw secrets

Files:

- `system/web/src/lib/api.ts`
- `system/web/src/pages/EditConfig.tsx`
- `system/web/src/hooks/setup/useConfigPrefill.ts`
- `system/web/src/pages/settings/ChannelSection.tsx`
- `system/web/src/pages/settings/STTSection.tsx`
- `system/web/src/pages/settings/TTSSection.tsx`
- `system/web/src/pages/settings/WifiSection.tsx`
- `system/web/src/pages/settings/MqttSection.tsx`

Actions:

1. Replace `DeviceConfig` GET type with sanitized config type.
2. Do not prefill raw secrets.
3. Show configured badges.
4. Use write-only secret inputs.
5. Send secret fields only when changed or explicitly cleared.

### Phase 2 — Remove raw OpenClaw config/token from browser

Files:

- `system/web/src/pages/monitor/index.tsx`
- `system/web/src/pages/GwConfig.tsx`
- `system/web/src/pages/monitor/ChatSection.tsx`

Actions:

1. Remove `/api/agent/config-json` calls from production UI.
2. Replace with redacted config summary.
3. Do not construct `/gw/chat#token=<raw-token>` from config.

### Phase 3 — Remove direct `/hw/*` access from browser

Files:

- `system/web/src/lib/api.ts`
- `system/web/src/pages/monitor/*`
- `system/web/src/pages/settings/*`
- `system/web/src/components/setup/*`

Actions:

1. Create typed `/api/hardware/*` wrappers.
2. Replace direct `/hw/*` fetches.
3. Remove `/hw/docs` iframe from production.
4. Serve private media via authenticated `/api/media/*`.

### Phase 4 — Auth-aware API client

Files:

- `system/web/src/lib/api.ts`
- new `system/web/src/lib/http.ts`
- `system/web/src/hooks/useEventSource.ts`
- `system/web/src/pages/monitor/CliSection.tsx`

Actions:

1. Centralize fetch with auth/error handling.
2. Decide auth transport:
   - httpOnly same-site cookie, or
   - bearer token in memory only.
3. Handle SSE auth carefully; native EventSource cannot set custom headers.
4. Remove shell UI from production or require one-time local ticket.

### Phase 5 — Browser hardening

Files:

- `scripts/provision/setup.sh`
- `scripts/imager/build.sh`
- possibly `system/web/index.html`

Actions:

1. Add CSP/security headers in nginx.
2. Add clickjacking protections.
3. Remove secret query params from redirects.
4. Add local-data clear UI.

---

## Route and UI risk matrix

| Frontend area | Current behavior | Risk | Recommended change |
|---|---|---|---|
| Edit Config | GET raw `/api/device/config` secrets | Secret dump into browser | Sanitized config + write-only secrets |
| Setup URL params | Reads `llm_api_key`, bot tokens, passwords from query | Secrets leak via URL/history/logs | One-time setup code; scrub query immediately |
| Gateway link | Fetches raw config, extracts gateway token, builds `#token=` URL | Gateway token in browser/URL | Backend session or local-only gateway |
| Gateway config page | Renders raw `openclaw.json` | Token/config leak | Redacted summary or local-only dev page |
| Hardware controls | Browser calls `/hw/*` directly | Raw hardware API exposed | Authenticated Go proxy endpoints |
| API Docs | iframe `/hw/docs` | Raw API execution from browser | Remove/dev-only/local-only |
| CLI | WebSocket `/api/system/shell` | Browser shell | Remove production; local-only dev flag |
| Logs | Fetch/stream logs | Secret/private data leak | Auth + redaction + limits |
| Chat history | localStorage persistence | Private text persists | TTL/clear/opt-in |
| TTS preview | Sends API key to `/hw/voice/speak` | Secret in hardware call/logs | Server-side preview endpoint |

---

## Suggested regression checks

### 1. No raw secrets in device config response

```sh
curl -s http://<device-ip>/api/device/config | grep -Ei 'api_key|token|password|secret'
```

Expected: no raw secrets.

### 2. No production frontend references to raw hardware API

```sh
grep -R '"/hw/\|`/hw/\|/hw/docs' system/web/src
```

Expected: no production direct calls, except documented dev-only gates.

### 3. No raw OpenClaw config/token path

```sh
grep -R 'config-json\|#token=' system/web/src
```

Expected: no production usage.

### 4. No shell in production build

```sh
cd system/web
npm run build
grep -R 'api/system/shell\|CliSection' dist || true
```

Expected: no shell references unless explicit dev flag enabled.

### 5. Secret query params scrubbed

Open:

```text
http://<device-ip>/setup?llm_api_key=SECRET&tele_token=SECRET
```

Expected:

- URL bar is scrubbed after load.
- Redirects do not preserve secret params.

### 6. Security headers present

```sh
curl -I http://<device-ip>/monitor
```

Expected:

- `Content-Security-Policy`
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`

---

## Files to edit checklist

### API contracts and fetch layer

- `system/web/src/lib/api.ts`
  - Split public config GET type from update/write type.
  - Export central `apiRequest` or move to `http.ts`.
  - Stop direct `/hw/voice/speak` TTS preview.

- New file: `system/web/src/lib/http.ts`
  - Central authenticated fetch wrapper.
  - Shared error/401 handling.

- New file: `system/web/src/lib/safeSearch.ts`
  - Strip secret query params before redirects/links.

### Config UI

- `system/web/src/pages/EditConfig.tsx`
  - Do not prefill raw secrets.
  - Use configured booleans and write-only fields.

- `system/web/src/hooks/setup/useConfigPrefill.ts`
  - Stop pre-filling secrets from server config.

- `system/web/src/pages/settings/ChannelSection.tsx`
  - Replace visible token fields with write-only secret update UX.

- `system/web/src/pages/settings/STTSection.tsx`
  - Write-only API key fields.

- `system/web/src/pages/settings/TTSSection.tsx`
  - Write-only API key fields.

- `system/web/src/pages/settings/WifiSection.tsx`
  - Do not display saved WiFi password.

- `system/web/src/pages/settings/MqttSection.tsx`
  - Do not display saved MQTT password.

### Setup flow

- `system/web/src/hooks/setup/useSetupUrlParams.ts`
  - Remove support for secret query params or scrub immediately.

- `system/web/src/hooks/setup/useSetupStatusPolling.ts`
  - Do not preserve secret query params during redirects.

- `system/web/src/App.tsx`
  - Do not preserve secret query params during LAN-IP redirect.

- `system/web/src/pages/setup/Setup.tsx`
  - Do not build mDNS links with raw `window.location.search`.

### Gateway/OpenClaw

- `system/web/src/pages/monitor/index.tsx`
  - Remove raw config fetch and `#token=` URL construction.
  - Remove/dev-gate `/hw/docs` iframe.

- `system/web/src/pages/GwConfig.tsx`
  - Use redacted config summary or remove from production.

- `system/web/src/pages/monitor/ChatSection.tsx`
  - Remove raw config-json usage.

### Hardware UI

- `system/web/src/pages/monitor/OverviewSection.tsx`
- `system/web/src/pages/monitor/CameraSection.tsx`
- `system/web/src/pages/monitor/ServoSection.tsx`
- `system/web/src/pages/monitor/BluetoothSection.tsx`
- `system/web/src/pages/monitor/FaceOwnersSection.tsx`
- `system/web/src/pages/settings/FaceSection.tsx`
- `system/web/src/pages/settings/VoiceSection.tsx`
- `system/web/src/pages/settings/VoiceSection.tsx`

Actions:

- Replace `/hw/*` calls with authenticated `/api/hardware/*` wrappers.
- Encode all dynamic path segments.
- Serve private media through authenticated `/api/media/*`.

### CLI/logs/local storage

- `system/web/src/pages/monitor/CliSection.tsx`
  - Remove from production or gate behind explicit dev flag.

- `system/web/src/pages/monitor/LogsSection.tsx`
  - Use authenticated fetch/SSE.
  - Expect redacted logs.

- `system/web/src/pages/monitor/ChatSection.tsx`
  - Add TTL/clear local history.

### Deployment headers

- `scripts/provision/setup.sh`
  - Add CSP/security headers to nginx.

- `scripts/imager/build.sh`
  - Mirror headers.

---

## Open decisions

1. Should remote web monitor be accessible on LAN after setup, or only local/Tailscale/admin-authenticated?
2. What auth should web use?
   - httpOnly same-site cookie is best for SSE/WebSocket compatibility.
   - Bearer token in JS memory is easier but worse for XSS.
3. Should setup links ever carry secrets?
   - Recommendation: no; use one-time setup/prefill code.
4. Should browser UI control raw hardware?
   - Recommendation: no; use Go API proxy with auth and validation.
5. Should web shell exist in production?
   - Recommendation: no.
6. Should gateway config be editable in browser?
   - Recommendation: redacted structured config only; raw config local dev only.

---

## Bottom line

The frontend currently behaves as a trusted local admin console, but it is served over a network-facing web surface. It should be refactored so the browser never receives raw long-lived secrets, never calls raw hardware APIs, never opens production shell access, and never handles raw OpenClaw gateway tokens. Server-side auth and local-only enforcement are mandatory, but the web app must also stop depending on unsafe endpoints and secret-returning responses.
