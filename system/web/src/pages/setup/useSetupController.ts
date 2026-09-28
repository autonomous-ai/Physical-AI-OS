import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { getNetworks, getSetupStatus, setupDevice } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useSetupUrlParams, clearStoredSetupParams } from "@/hooks/setup/useSetupUrlParams";
import { useTTSCatalog } from "@/hooks/setup/useTTSCatalog";
import { useConfigPrefill } from "@/hooks/setup/useConfigPrefill";
import { useSetupStatusPolling } from "@/hooks/setup/useSetupStatusPolling";
import { useFaceEnroll } from "@/hooks/setup/useFaceEnroll";
import { useWifiConnected } from "@/hooks/setup/useWifiConnected";
import { useCapabilities } from "@/hooks/useCapabilities";
import { Cap } from "@/pages/monitor/types";
import { setupBridge } from "@/lib/setupBridge";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "@/hooks/setup/types";
import type { ChannelType, NetworkItem, SetupChannelType } from "@/types";
import { normaliseSetupError, type SetupMode } from "./helpers";

// useSetupController owns ALL of the Setup page's state, effects, derived
// values, and handlers. The Setup component consumes what it returns and only
// renders — no business logic lives in the JSX. This keeps the large wizard's
// data flow in one testable place and the view thin. Behavior is identical to
// the previous single-file page; this is a pure UI/logic split.
export function useSetupController(mode: SetupMode) {
  // #force in App.tsx forces mode="initial" for UI testing, but the device's
  // backend is still reachable in that scenario — so for feature-gating we
  // treat #force the same as continue (show Voice/Face sections, allow
  // prefill-driven checks, etc.). The redirect logic still keys off the raw
  // mode flag below since it should not auto-bounce during force testing.
  const forceHash = typeof window !== "undefined" && window.location.hash === "#force";
  const isContinue = mode === "continue" || forceHash;
  // Dev hosts (localhost / 127.0.0.1) are local Vite servers pointed at a
  // remote device — auto-bouncing to /monitor while debugging Setup is annoying.
  const isLocalDev = typeof window !== "undefined" &&
    (window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1");
  const [theme, toggleTheme, themeClass] = useTheme();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  useDocumentTitle("Setup");

  const channelParam = searchParams.get("channel");
  const initialChannel: SetupChannelType =
    channelParam === "telegram" || channelParam === "slack" || channelParam === "discord"
      ? (channelParam as ChannelType)
      : "none";
  const [channel, setChannel] = useState<SetupChannelType>(initialChannel);

  const urlParams = useSetupUrlParams();

  // When the OS server (golang) pushes provisioning credentials via query params, the
  // operator only needs to pick a Wi-Fi — every other field is already filled.
  // Treat presence of llm_api_key as the signal the OS server handed us a full config:
  // hide the AI Brain / Channels / Language / TTS menu entries and keep those
  // sections mounted (display:none) so their state still submits with the form.
  // Gated to initial (AP) mode so editing on the LAN IP keeps the full menu.
  const devicePushedConfig = mode === "initial" && !!urlParams.llmApiKey;

  // Language + Lamp's Voice are gated behind ?debug=true: regular operators
  // get the auto-detected language and the "Rachel"/elevenlabs voice defaults,
  // which still flow through submit because the sections stay in the DOM
  // (display:none) — same pattern as STT/MQTT below.
  const debug = searchParams.get("debug") === "true";

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Per-step validation hint shown under the wizard buttons when Next is
  // blocked. Distinct from `error` (submit-level / backend errors) so the two
  // don't clobber each other. Cleared on any successful navigation.
  const [stepError, setStepError] = useState<string | null>(null);
  const [setupWorking, setSetupWorking] = useState(false);
  // The run this tab submitted carried no SSID, so the backend took the wired
  // branch: verify the existing uplink, tear down the AP, no Wi-Fi join. Latched
  // at submit rather than read live off the form so the progress screen keeps
  // describing the run it is actually reporting on.
  const [wiredRun, setWiredRun] = useState(false);
  // True when this tab adopted a `failed` verdict left behind by a PREVIOUS
  // attempt (see the mount effect below). Kept separate from setupWorking on
  // purpose: setupWorking drives the live pollers/ticker/bridge emits for a
  // join this tab actually started, while this flag only says "show the failure
  // screen". Cleared by the retry action, which returns the operator to the
  // form.
  const [adoptedFailure, setAdoptedFailure] = useState(false);
  // Setup phase mirrors the backend SetupStatus enum: connecting → connected
  // (success path) or failed. Drives the post-submit screen UI.
  const [setupPhase, setSetupPhase] = useState<"connecting" | "connected" | "failed">("connecting");
  const [setupLanIP, setSetupLanIP] = useState<string>("");
  const [setupErrorMsg, setSetupErrorMsg] = useState<string>("");
  // Seconds elapsed since the join started, shown on the connecting screen so
  // the wait feels measured rather than open-ended. Reset + ticked by the
  // effect below whenever we enter the connecting phase.
  const [elapsed, setElapsed] = useState(0);
  // Single-step wizard: Wi-Fi is the only visible section on initial setup.
  const [activeSection, setActiveSection] = useState<SectionId>("wifi");
  const contentRef = useRef<HTMLDivElement>(null);

  const [deviceId, setDeviceId] = useState(urlParams.deviceId || "");
  const [mac, setMac] = useState("");
  // Admin password state. The Wi-Fi step keeps this input mounted but hidden;
  // when the operator submits it empty (the default UX now), the backend
  // (handler.Setup) fills it with the 4-char hardware suffix from
  // device.GetDeviceMac(), and the operator reads that suffix off the sticker
  // on the bottom of the device to sign into /login later. If autofill or a
  // deep-linked value happens to populate this, the backend takes it verbatim.
  const [adminPassword, setAdminPassword] = useState("");
  // hasAdminPassword mirrors cfg.has_admin_password from /api/device/config.
  // True = device already has a bcrypt hash on file → hide the admin-password
  // fields and don't require them. False = first-time or migration device
  // missing the hash → show + require so the operator can't ship a setup
  // submit without one. Starts true to avoid flashing the fields during the
  // probe; useConfigPrefill flips it to false when the server reports missing.
  const [hasAdminPassword, setHasAdminPassword] = useState(true);
  // Mirrors cfg.has_network_password — when true, WifiSection swaps the
  // password input for a "configured" hint so the operator doesn't have to
  // retype a saved Wi-Fi password during re-setup via `#force`. Submit ships
  // an empty password; server merges from cfg.NetworkPassword pre-validation.
  const [hasNetworkPassword, setHasNetworkPassword] = useState(false);
  // mDNS hostname for the device on home Wi-Fi: `<device_type>-<suffix>.local`.
  // Matches what stage_ap sets via `hostnamectl set-hostname ${DEVICE_TYPE}-${SUFFIX_LC}`
  // — both sides derive from the device's hardware ID (Pi device-tree serial /
  // cpuinfo Serial / eth0 MAC) and the DEVICE_TYPE class, via the same logic in
  // system/device/hardware.go.
  //
  // The backend returns `cfg.mac` already formatted as "<device_type>-XXXX" and
  // lowercase (see GetDeviceMac()), so the lowercased mac IS the hostname. Derive
  // the prefix from mac — never hardcode "lamp-" — so the redirect follows the
  // device class (lamp-a1b2 / intern-3c4d). The 4-char hex tail is validated to
  // guard against an empty/partial config. Empty when the config hasn't returned
  // yet, or when the device couldn't determine a serial.
  const deviceMdnsHost = useMemo(() => {
    const host = (mac || "").trim().toLowerCase();
    if (!/^[0-9a-f]{4}$/.test(host.slice(-4))) return "";
    return host;
  }, [mac]);
  // Device-type prefix ("lamp-" / "intern-") split off the same mac, used as the
  // router-admin search hint when the full mDNS host isn't usable yet. Follows
  // whatever device is actually being set up — never a hardcoded class.
  const deviceTypePrefix = useMemo(() => {
    const m = (mac || "").trim().toLowerCase();
    const dash = m.lastIndexOf("-");
    return dash > 0 ? m.slice(0, dash + 1) : "";
  }, [mac]);
  // The device class itself ("lamp", "intern-v2", …) — deviceTypePrefix without
  // its trailing dash. `mac` is "<device_type>-<4 hex>" (GetDeviceMac), and the
  // class may itself contain dashes ("intern-v2-d94b"), which is why both derive
  // from lastIndexOf("-") rather than splitting on the first one. Empty until
  // the config resolves.
  const deviceType = useMemo(
    () => deviceTypePrefix.replace(/-$/, ""),
    [deviceTypePrefix],
  );
  // Devices that skip the Wi-Fi failure screen entirely and drop the operator
  // straight back on the form (equivalent to auto-pressing "Back to Wi-Fi").
  // Product decision, per device class — every other device keeps the screen
  // with its error text and checklist.
  const skipsFailureScreen = deviceType === "intern-v2";
  const [llmApiKey, setLlmApiKey] = useState(urlParams.llmApiKey || "");
  const [llmUrl, setLlmUrl] = useState(urlParams.llmUrl || "");
  const [llmModel, setLlmModel] = useState(urlParams.llmModel || "");
  // Snapshot of AI Brain fields populated when entering setup (URL or saved
  // config). Populated values render with the Edit pencil so re-running setup
  // doesn't accidentally overwrite credentials.
  const [llmLoaded, setLlmLoaded] = useState<LlmLoadedState>({
    apiKey: !!urlParams.llmApiKey,
    baseUrl: !!urlParams.llmUrl,
    model: !!urlParams.llmModel,
  });
  const [llmDisableThinking, setLlmDisableThinking] = useState(false);
  // deepgram input is hidden in this build; submit reads urlParams.deepgramApiKey directly
  const [ttsApiKey, setTtsApiKey] = useState(urlParams.ttsApiKey || "");
  const [ttsBaseUrl, setTtsBaseUrl] = useState(urlParams.ttsBaseUrl || "");
  // STT credentials are not exposed in Setup UI but still saved to config so
  // the device's voice pipeline has fallback values mirroring the LLM endpoint.
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
  // Pre-fill STT language from URL param, else browser locale so VN/CN buyers
  // don't have to touch this field; users can still override before submitting.
  // URL value is validated against the dropdown allow-list — server stores
  // anything we send (no validation upstream), so we gate at the FE boundary.
  // Final fallback is "en" (rather than empty) so the saved config always has
  // a sensible default the agent can lean on.
  const [sttLanguage, setSttLanguage] = useState<string>(() => {
    const VALID = ["en", "vi", "zh-CN", "zh-TW"];
    if (urlParams.sttLanguage) {
      if (VALID.includes(urlParams.sttLanguage)) return urlParams.sttLanguage;
      console.warn(`[setup] URL stt_language="${urlParams.sttLanguage}" not in ${VALID.join(",")}, ignoring`);
    }
    const loc = (navigator.language || "").toLowerCase();
    if (loc.startsWith("vi")) return "vi";
    if (loc.startsWith("zh-tw") || loc.startsWith("zh-hant") || loc.startsWith("zh-hk")) return "zh-TW";
    if (loc.startsWith("zh")) return "zh-CN";
    if (loc.startsWith("en")) return "en";
    return "en";
  });
  const [ttsProvider, setTtsProvider] = useState(urlParams.ttsProvider || "elevenlabs");
  const [ttsVoice, setTtsVoice] = useState(urlParams.ttsVoice || "Rachel");
  const { ttsProviders, ttsVoices } = useTTSCatalog({
    ttsProvider, sttLanguage, ttsVoice,
    urlProvider: urlParams.ttsProvider,
    urlVoice: urlParams.ttsVoice,
    setTtsProvider, setTtsVoice,
  });
  const [teleToken, setTeleToken] = useState(urlParams.teleToken || "");
  const [teleUserId, setTeleUserId] = useState(urlParams.teleUserId || "");
  const [slackBotToken, setSlackBotToken] = useState(urlParams.slackBotToken || "");
  const [slackAppToken, setSlackAppToken] = useState(urlParams.slackAppToken || "");
  const [slackUserId, setSlackUserId] = useState(urlParams.slackUserId || "");
  const [discordBotToken, setDiscordBotToken] = useState(urlParams.discordBotToken || "");
  const [discordGuildId, setDiscordGuildId] = useState(urlParams.discordGuildId || "");
  const [discordUserId, setDiscordUserId] = useState(urlParams.discordUserId || "");
  // Optional admin-supplied iMessage caller-context prompt. Non-secret
  // plaintext; hydrated by useConfigPrefill and rendered in ChannelSection.
  const [bluebubblesCallerContext, setBluebubblesCallerContext] = useState("");
  // Snapshot of channel credentials populated when entering Setup. Filled
  // values render with the Edit pencil to prevent accidental overwrites.
  const [channelLoaded, setChannelLoaded] = useState<ChannelLoadedState>({
    teleToken: !!urlParams.teleToken, teleUserId: !!urlParams.teleUserId,
    slackBotToken: !!urlParams.slackBotToken, slackAppToken: !!urlParams.slackAppToken,
    slackUserId: !!urlParams.slackUserId,
    discordBotToken: !!urlParams.discordBotToken, discordGuildId: !!urlParams.discordGuildId,
    discordUserId: !!urlParams.discordUserId,
    // iMessage / BlueBubbles never rides through URL params (secrets in URLs
    // is a footgun), so these are always false at Setup entry — the operator
    // pastes them into ChannelSection which then flips these true on next
    // config load. Included here to keep the object shape aligned with
    // ChannelLoadedState.
    bluebubblesServerUrl: false, bluebubblesPassword: false, bluebubblesUserAddress: false,
    // Same story for caller-context: never carried in URL params, filled in
    // by useConfigPrefill once /api/device/config returns.
    bluebubblesCallerContext: false,
  });
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");

  // Enrolled owners list (faces + voice samples), shared by the Voice and Face
  // sections. Enroll/upload/remove logic now lives inside those shared Settings
  // components; Setup only needs the list + a reload fn to drive sectionDone and
  // feed both sections. Only meaningful in continue mode (device online).
  const { faceOwners, loadFaceOwners } = useFaceEnroll();

  // Truth about whether the device is already on home Wi-Fi, read from the
  // device's live network state (internet + associated SSID) — NOT from the
  // ephemeral form fields, which reset to empty on the full page reload the
  // AP→STA redirect performs. This is what marks the Wi-Fi step done after the
  // device joins Wi-Fi and the page reloads on the new LAN-IP origin, instead
  // of re-prompting "choose your Wi-Fi + password". See useWifiConnected.
  const { wifiConnected, wiredUplink, currentSsid, checking: wifiChecking } = useWifiConnected();

  // The enrollment steps are hardware, not preference: "My Voice" records the
  // operator through the mic, "Face" photographs them through the camera. A
  // device that doesn't declare the capability has no way to complete either,
  // so the wizard must not offer it — an Intern (audio + sensing + light, no
  // `vision`) was still being asked to enroll a face it cannot see. Same
  // contract Monitor gates its tabs on: os-server parses ROBOT.md and serves
  // the declared list on /api/system/info. Fail-open while it loads (hasCap
  // returns true for an unknown set), matching every other gate in the web.
  const { hasCap } = useCapabilities();
  const canEnrollVoice = hasCap(Cap.Audio);
  const canEnrollFace = hasCap(Cap.Vision);

  // Per-section "done" detection drives the ✓ checkmark in the sidebar and
  // the auto-scroll-to-next-pending behavior in continue mode. We treat a
  // section as done when its config has the value the user came here to set.
  // Secret fields don't round-trip through GET /api/device/config anymore
  // (ConfigPublicResponse returns has_* booleans only), so check `*Loaded`
  // alongside the raw form value — operator typing into the field also
  // counts as done, but a saved-but-not-retyped secret still shows the green
  // tick from its presence boolean.
  const sectionDone: Record<SectionId, boolean> = {
    // Device is no longer a wizard step. Kept in Record<SectionId,…> because
    // SectionId still lists "device" (settings/edit mode uses it). Always
    // satisfied here so the initial-setup progress bar counts correctly.
    device: true,
    // Wi-Fi step needs a network + a Wi-Fi password (or one on file). The
    // admin password field is hidden and defaulted server-side to the device
    // hardware suffix (handler.Setup), so it's not part of the gate here.
    //
    // `wifiConnected` short-circuits this to done regardless of the form
    // fields: after the AP→STA join reloads the page on the new LAN-IP origin,
    // ssid/password reset to empty, but the device IS on home Wi-Fi — so the
    // live-state signal (internet + associated SSID) is the authoritative
    // "Wi-Fi is set up" truth, not the wiped form input.
    // `wiredUplink` satisfies it too: the device already reaches the network
    // over ethernet, so there are no Wi-Fi credentials to collect and the step
    // must not block setup.
    wifi: wifiConnected || wiredUplink || (!!ssid && (!!password || hasNetworkPassword)),
    llm: !!llmApiKey || llmLoaded.apiKey,
    language: true, // Auto/empty is a valid choice — never block on this.
    realtime: true, // Not a setup step (edit-only card); never blocks setup.
    channel: true,
    tts: !!ttsVoice,
    voice: faceOwners.some((p) => (p.voice_samples?.length ?? 0) > 0),
    face: faceOwners.some((p) => p.photo_count > 0),
    deepgram: true,
    mqtt: true,
    stt: true, // EditConfig's alias for language; not rendered in Setup.
  };

  useEffect(() => {
    setMqttEndpoint((prev) => prev || urlParams.mqttEndpoint);
    setMqttPort((prev) => prev || urlParams.mqttPort);
    setMqttUsername((prev) => prev || urlParams.mqttUsername);
    setMqttPassword((prev) => prev || urlParams.mqttPassword);
    setFaChannel((prev) => prev || urlParams.faChannel);
    setFdChannel((prev) => prev || urlParams.fdChannel);
  }, [urlParams]);

  // Prefill the Wi-Fi picker with the network the device is actually joined to
  // (from useWifiConnected) so the reloaded page shows the live SSID instead of
  // an empty "choose your Wi-Fi". Fill-only (`prev || …`) — never clobbers a
  // network the operator is mid-selecting or one already prefilled from config.
  useEffect(() => {
    if (currentSsid) setSsid((prev) => prev || currentSsid);
  }, [currentSsid]);

  // Continue mode: refresh enrolled face/voice owners (device is online now).
  useEffect(() => {
    if (isContinue) loadFaceOwners();
  }, [isContinue, loadFaceOwners]);

  // Continue mode: scroll the user to the first section that still needs
  // attention so they can see what's left to do without hunting through
  // the sidebar. If every required section is already done on first load,
  // bounce straight to /monitor — Setup has nothing left to ask for.
  // autoScrolledRef doubles as the "we previously saw at least one incomplete
  // section" flag. Redirect only fires after we've scrolled the user to a
  // pending section at least once — i.e. they were actively running setup,
  // and the last pending field just became done. When the user opens /setup
  // post-completion to view/edit, the flag stays false, so we stay on the
  // page with checks visible instead of bouncing them to /monitor.
  const autoScrolledRef = useRef(false);
  // `setup_done` — the wizard's terminal event, and the one a parent window
  // waits on to close the popup. It fires on every route out of a finished
  // wizard: the operator pressing the last-step button (whether that button
  // says "Skip & finish" or "Go to monitor"), and the auto-bounce to /monitor
  // when nothing was left to do. Previously only the skip variant emitted it,
  // so an operator who actually COMPLETED the last optional step — or a device
  // that arrived with every step satisfied, which is the normal shape of a
  // re-opened popup after a wired setup — left the parent with no signal.
  // Ref-guarded: several of those routes can run in the same tick.
  const setupDoneRef = useRef(false);
  const finishWizard = useCallback(() => {
    if (setupDoneRef.current) return;
    setupDoneRef.current = true;
    setupBridge.setupDone();
  }, []);
  // Set by the deep-link effect when the URL hash (#voice / #face / …) named a
  // valid, visible step on load. When true, the auto-scroll below must NOT hijack
  // activeSection to the first-incomplete step — the operator asked for a
  // specific tab and we honor it. The all-done → /monitor bounce still applies.
  const deepLinkedRef = useRef(false);
  // True while the URL hash names a step that isn't visible yet — i.e. the mode
  // hasn't resolved, so we can't know whether that step is about to exist. Setup
  // renders a skeleton in this window instead of falling back to Wi-Fi and then
  // correcting itself, which is what made the requested tab appear seconds late.
  // Seeded from the initial hash so the very first paint is already correct: a
  // hash naming anything other than the always-present `wifi` step is a deep-link
  // whose target may still be pending.
  const [awaitingDeepLink, setAwaitingDeepLink] = useState(() => {
    if (typeof window === "undefined") return false;
    const h = window.location.hash.replace(/^#/, "");
    return !!h && h !== "force" && h !== "wifi";
  });
  useEffect(() => {
    if (!isContinue) return;
    if (!llmApiKey) return; // wait until config has loaded
    // Wait for the live network probe before judging any step. `sectionDone.wifi`
    // is false until useWifiConnected answers, and its first probe lands AFTER
    // this effect's first run — so without this gate the search below always saw
    // Wi-Fi as incomplete, parked the operator there, and spent autoScrolledRef,
    // which then blocked the correction when the probe came back a moment later.
    // A wired device felt this hardest: it has nothing to enter on that step, yet
    // the wizard pinned it to the Wi-Fi picker and never moved on.
    if (wifiChecking) return;
    // An enrollment the device can't perform is not a pending step. Leaving
    // `face` in here on a camera-less device meant `required` could never be
    // fully satisfied, so the "everything is done" branch — the one that emits
    // setup_done and bounces to /monitor — was unreachable for that device.
    const required: SectionId[] = ["wifi", "llm", "tts",
      ...(canEnrollVoice ? ["voice" as SectionId] : []),
      ...(canEnrollFace ? ["face" as SectionId] : []),
    ];
    // Redirect any time all required sections become done — including later
    // ticks when async data (e.g. faceOwners) arrives after first paint. This
    // path is NOT gated by autoScrolledRef on purpose; otherwise the first
    // effect run (before faceOwners loaded) sets the ref and the redirect
    // never fires once enrollment counts come back.
    if (required.every((id) => sectionDone[id])) {
      // Skip auto-bounce when user is on #force testing the UI on a
      // provisioned device, or when running on a local dev host pointed at a
      // remote device — they want to see the page, not jump away.
      if (autoScrolledRef.current && !forceHash && !isLocalDev) {
        // Leaving the wizard with everything satisfied IS the end of setup —
        // tell the opener before navigating, or a popup that completes this way
        // (nothing left to enroll) would never emit a terminal event and the
        // parent would keep it open forever.
        finishWizard();
        navigate("/monitor", { replace: true });
      }
      return;
    }
    if (autoScrolledRef.current) return;
    // A deep-link (#voice / #face / …) is an explicit request for that tab —
    // don't override it by jumping to the first incomplete step. Mark the
    // auto-scroll as spent so the all-done redirect above can still fire later.
    if (deepLinkedRef.current) { autoScrolledRef.current = true; return; }
    const order: SectionId[] = ["wifi", "llm", "channel", "language", "tts",
      ...(canEnrollVoice ? ["voice" as SectionId] : []),
      ...(canEnrollFace ? ["face" as SectionId] : []),
    ];
    const next = order.find((id) => !sectionDone[id]) ?? "tts";
    setActiveSection(next);
    autoScrolledRef.current = true;
    // `forceHash` and `isLocalDev` are read straight off window.location at
    // render time, not React state — they are escape hatches that only
    // suppress the auto-bounce. Listing them would make a hash change re-enter
    // this navigation effect (it calls navigate()/setActiveSection()), so they
    // stay out on purpose; the effect always sees the current render's values.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isContinue, llmApiKey, wifiChecking, canEnrollVoice, canEnrollFace, sectionDone, navigate, finishWizard]);

  // Wi-Fi scan with retry — kept inline since it's specific to this page.
  // Runs once on mount; the operator can re-fire via the picker's "Refresh"
  // button (see refreshNetworks below) — critical right after a soft reset,
  // where wlan0 comes up in AP mode with no cached scan results and the first
  // scan often returns empty.
  const runScan = useCallback(() => {
    setLoadingList(true);
    const maxAttempts = 4;
    let attempt = 0;
    function fetchNetworks(): Promise<void> {
      attempt += 1;
      return getNetworks()
        .then((nets) => setNetworks((nets ?? []).filter((n) => n.ssid !== "")))
        .catch(() => { if (attempt < maxAttempts) return fetchNetworks(); setNetworks([]); });
    }
    return fetchNetworks().finally(() => setLoadingList(false));
  }, []);

  useEffect(() => {
    runScan();
  }, [runScan]);

  // Exposed to WifiSection. Silently no-ops while a scan is in flight to
  // avoid stacking parallel `iw scan` calls that fight for the radio.
  const refreshNetworks = useCallback(() => {
    if (loadingList) return;
    runScan();
  }, [loadingList, runScan]);

  useConfigPrefill({
    urlParams, channelParam,
    setTtsProvider, setTtsVoice, setSsid, setDeviceId, setMac, setActiveSection,
    setLlmUrl, setLlmModel, setLlmLoaded, setLlmDisableThinking,
    setTtsBaseUrl,
    setChannelLoaded,
    setTeleUserId,
    setSlackUserId,
    setDiscordGuildId, setDiscordUserId,
    setChannel,
    setMqttEndpoint, setMqttPort, setMqttUsername,
    setFaChannel, setFdChannel,
    setSttLanguage,
    setBluebubblesCallerContext,
    setHasAdminPassword,
    setHasNetworkPassword,
  });

  useSetupStatusPolling({
    setupWorking, setupPhase, setupLanIP, elapsed,
    mdnsHost: deviceMdnsHost,
    setSetupPhase, setSetupLanIP, setSetupErrorMsg,
  });

  // Recover a failed join that this tab never witnessed.
  //
  // The wrong-password path strands the operator: the AP dies ~2s after submit,
  // so the phase poll stops reaching the device long before the backend reaches
  // its verdict at t≈62s. Whether they closed the tab, the popup was reopened by
  // the companion app, or they simply reloaded once the AP came back, the fresh
  // page mounts with setupWorking=false and renders a blank Wi-Fi form — no hint
  // that the previous attempt failed or why.
  //
  // The backend still holds that verdict in setupState (in-memory, survives the
  // AP→STA→AP bounce) and /api/device/setup/status is public — no admin session
  // needed, which matters because a device that failed setup never wrote an
  // admin password hash. So: read it once on mount and, if the last attempt
  // failed, drop straight into the failure screen carrying the backend's real
  // reason instead of the hedged client-side guess.
  //
  // Only "failed" is adopted. "connecting" is deliberately ignored: a stale
  // in-flight phase from a previous tab would hijack this one into a progress
  // screen it can't drive (its poller never ran, elapsed starts at 0), and the
  // operator would be stuck watching a counter for a join nobody is tracking.
  const failureAdoptedRef = useRef(false);
  useEffect(() => {
    if (failureAdoptedRef.current) return;
    // A submit from THIS tab owns the screen — its own poller is authoritative.
    if (setupWorking) return;
    // Continue mode means SetupGate already proved the device is online and
    // serving this page from its LAN address. A `failed` verdict left in the
    // backend's in-memory setupState by some earlier attempt cannot describe
    // that device — nothing ever resets the phase to idle (see
    // system/device/setup.go), so it survives indefinitely. Adopting it here
    // wiped the form and dropped the operator back on the Wi-Fi step of a
    // device whose network is demonstrably fine, and emitted `setup_failed` to
    // the parent for a setup that had already succeeded. The adoption path
    // exists for the offline case — a failed join leaves the device back on its
    // AP with no internet, which resolves to initial mode, where this still runs.
    if (isContinue) return;
    let cancelled = false;
    getSetupStatus().then((s) => {
      if (cancelled || s.phase !== "failed") return;
      failureAdoptedRef.current = true;
      // Decide from THIS response's mac rather than the `mac` state, which is
      // still empty on mount (useConfigPrefill fills it from a separate
      // request). Waiting for that would let the failure screen paint for a
      // beat on a device that is supposed to skip it.
      const macType = (s.mac || "").trim().toLowerCase().replace(/-[0-9a-f]{4}$/, "");
      const skipScreen = macType === "intern-v2";
      // Wipe every field the failed attempt left behind, so rejoining the AP
      // lands the operator on a genuinely clean wizard rather than one
      // pre-filled from an attempt that never provisioned anything. A Wi-Fi
      // failure bails before device.Setup writes any config, so none of this
      // was ever persisted device-side — carrying it forward would only
      // resubmit the credentials that just failed.
      //
      // The Wi-Fi password is the important one: leaving it would let the
      // operator resubmit the same wrong value without noticing. The SSID goes
      // too, so the picker re-resolves from the fresh scan (and from
      // useConfigPrefill / useWifiConnected) instead of pinning a stale choice.
      setSsid("");
      setPassword("");
      setAdminPassword("");
      setError(null);
      setStepError(null);
      setSetupLanIP("");
      setActiveSection("wifi");
      // Also drop the persisted params snapshot: sessionStorage survives F5, so
      // an operator who rejoins the AP and reloads this same tab would
      // otherwise rehydrate the failed attempt's query string. Clearing it here
      // (rather than reloading now) keeps the current document usable for the
      // in-place "Back to Wi-Fi" retry while guaranteeing the NEXT load starts
      // from nothing.
      clearStoredSetupParams();
      // Devices that skip the failure screen stop here: the state above is
      // already the clean Wi-Fi form we want them on, so we simply never raise
      // the failure flags. Leaving setupPhase at "connecting" (its initial
      // value) also keeps the auto-return effect below from firing for a
      // failure this tab never displayed.
      //
      // The bridge emit is fired directly because the phase-transition effect
      // that normally sends it is gated on setupWorking, which we deliberately
      // never raise here. Without this the parent would get no signal at all
      // that the previous join failed — the screen is hidden from the operator,
      // not from the companion app.
      if (skipScreen) {
        setupBridge.failed(s.error || "Wi-Fi setup failed.");
        return;
      }
      setSetupErrorMsg(s.error || "Wi-Fi setup failed.");
      setSetupPhase("failed");
      // `adoptedFailure` — NOT setupWorking — is what shows the failure screen
      // here. setupWorking means "this tab has a join in flight": it starts the
      // 600ms phase poll, the elapsed ticker, and the bridge's connecting/
      // failed emits. Setting it for a join we merely read about would restart
      // that poll against a backend still reporting phase="failed" (nothing
      // resets it to idle — see system/device/setup.go), so the poll would
      // re-assert "failed" on a 600ms loop and bounce the operator straight
      // back out of the Wi-Fi form every time they pressed "Back to Wi-Fi".
      setAdoptedFailure(true);
    }).catch(() => {
      /* status unreachable (device rebooting / mid AP restore) — leave the
         form as-is; the operator can still retry from a clean wizard. */
    });
    return () => { cancelled = true; };
    // Mount-only, like the other setup hooks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Parent-window event bridge (autonomous.ai) ──────────────────────────────
  // Notify whoever opened this popup of each meaningful Setup milestone via
  // postMessage (see lib/setupBridge.ts). All emits are best-effort no-ops when
  // there's no opener, so they never affect the flow. Each milestone is fired
  // from a focused effect so it tracks the real state transition exactly once.

  // Wizard mounted and ready.
  useEffect(() => {
    setupBridge.opened({ mode, deviceId, mac });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // (The deep-link effect used to sit here. It now reads `visibleSections` as a
  // dependency, so it lives just below that declaration instead.)
  // Operator moved to a new step.
  useEffect(() => { setupBridge.stepChanged(activeSection); }, [activeSection]);
  // A WiFi network was chosen.
  useEffect(() => { if (ssid) setupBridge.wifiSelected(ssid); }, [ssid]);
  // Validation / backend error surfaced.
  useEffect(() => { if (error) setupBridge.error(error); }, [error]);
  // Post-submit phase transitions: connecting → connected | failed.
  useEffect(() => {
    if (!setupWorking) return;
    if (setupPhase === "connecting") setupBridge.connecting();
    else if (setupPhase === "connected") setupBridge.connected({ mdns_host: deviceMdnsHost, lan_ip: setupLanIP });
    else if (setupPhase === "failed") setupBridge.failed(setupErrorMsg || "Wi-Fi setup failed.");
  }, [setupWorking, setupPhase, deviceMdnsHost, setupLanIP, setupErrorMsg]);


  // Auto-mirror AI Brain key/URL into TTS while TTS field is empty.
  // Once the user types into TTS the sync stops; clearing it re-enables mirroring.
  useEffect(() => {
    if (!ttsApiKey && llmApiKey) setTtsApiKey(llmApiKey);
  }, [llmApiKey, ttsApiKey]);
  useEffect(() => {
    if (!ttsBaseUrl && llmUrl) setTtsBaseUrl(llmUrl);
  }, [llmUrl, ttsBaseUrl]);
  // Same for STT (no UI in Setup — silently mirrors LLM into config).
  useEffect(() => {
    if (!sttApiKey && llmApiKey) setSttApiKey(llmApiKey);
  }, [llmApiKey, sttApiKey]);
  useEffect(() => {
    if (!sttBaseUrl && llmUrl) setSttBaseUrl(llmUrl);
  }, [llmUrl, sttBaseUrl]);

  const scrollTo = (id: SectionId) => {
    setActiveSection(id);
    setStepError(null); // moving to any section clears a stale per-step hint
    // Mirror the active step into the URL hash (#wifi / #voice / #face / …) so
    // the address bar tracks the tab and the step is deep-linkable/shareable.
    // Preserve the `#force` test flag — never clobber it with a step hash, since
    // isContinue keys off it (see forceHash above).
    if (typeof window !== "undefined" && window.location.hash !== "#force") {
      // replaceState (not location.hash =) avoids piling up history entries so
      // the browser Back button still exits Setup instead of walking tabs.
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
    }
    // Pop the content area back to the top so a Back/Next click never lands
    // the operator mid-scroll of the previous section.
    contentRef.current?.scrollTo({ top: 0 });
  };

  // Default operator path: the OS server parent pushes config via URL params, so
  // AI Brain / Channels never need to be touched manually — sidebar entries
  // for them stay hidden unless ?debug=true. Manual fresh setup without
  // pushed params also requires ?debug=true to reach those sections.
  // STT (Deepgram) / MQTT are intentionally hidden — their state is still
  // wired up and submitted with empty or URL-prefilled defaults, so
  // re-adding a SectionCard + a SECTIONS entry brings them back without
  // other plumbing.
  // `optional` flags steps the operator can skip — drives the "Optional"
  // sidebar tag and the Skip button so they don't feel like a blocking
  // requirement. Required steps omit the flag.
  const SECTIONS: { id: SectionId; label: string; optional?: boolean }[] = [
    // Admin password no longer has a dedicated step — it's hidden in the merged
    // Wi-Fi step and defaulted server-side to the device's hardware suffix
    // (see handler.Setup). Device ID / MAC remain read-only metadata carried
    // through submit via component state (no DOM section needed).
    { id: "wifi", label: "Wi-Fi" },
    ...(debug ? [
      { id: "llm" as SectionId, label: "AI Brain" },
      { id: "channel" as SectionId, label: "Channels", optional: true },
      { id: "language" as SectionId, label: "Language" },
      { id: "tts" as SectionId, label: "Voice" },
    ] : []),
    // Voice / Face appear in continue mode only — they need the device's
    // hardware + backend, both unavailable while we're still on the AP. Both
    // are optional enrollments the operator can skip, and each is further gated
    // on the capability that makes it possible at all (mic / camera).
    ...(isContinue && canEnrollVoice ? [
      { id: "voice" as SectionId, label: "My Voice", optional: true },
    ] : []),
    ...(isContinue && canEnrollFace ? [
      { id: "face" as SectionId, label: "Face", optional: true },
    ] : []),
  ];

  // When the OS server pushed config, the operator only needs Device + Wi-Fi visible —
  // the rest are filled from URL and submitted silently. Sections remain in
  // the DOM (see `devicePushedConfig` display:none wrappers below) so values
  // still flow through the form; we just hide the menu entries.
  const visibleSections = devicePushedConfig
    ? SECTIONS.filter((s) => s.id === "wifi")
    : SECTIONS;

  // Deep-link: open the step named by the URL hash (#wifi / #voice / #face / …).
  // Only honor a hash that maps to a currently-visible section so a stale/hidden
  // id can't strand the operator on a blank step; `#force` is a test flag, not a
  // step, so it's skipped.
  //
  // Re-runs as visibleSections grows instead of firing once on mount. Voice/Face
  // only exist in continue mode, and SetupGate resolves that mode from two awaited
  // requests (checkInternet + getSetupStatus) — on a slow network Setup mounts
  // first, so a mount-only effect saw a sections list without `voice`, fell into
  // the seed branch below, and pinned the operator to Wi-Fi. Worse, seeding wrote
  // `#wifi` over the requested `#voice`, destroying the only record of the target
  // tab, so no later pass could recover it.
  useEffect(() => {
    if (typeof window === "undefined") return;
    // Already honored — never re-apply. Otherwise a later sections change would
    // yank the operator back off a tab they navigated to by hand.
    if (deepLinkedRef.current) return;
    const fromHash = window.location.hash.replace(/^#/, "");
    // Keep the `#force` test flag as-is — it's a mode toggle, not a step hash.
    if (fromHash === "force") return;
    if (fromHash && visibleSections.some((s) => s.id === fromHash)) {
      // Record the explicit deep-link so the continue-mode auto-scroll doesn't
      // later yank the operator off this tab onto the first incomplete step.
      deepLinkedRef.current = true;
      setAwaitingDeepLink(false);
      scrollTo(fromHash as SectionId); // deep-link into the named step
      return;
    }
    // A hash naming a step that isn't visible yet means the mode is still
    // resolving. Leave the URL untouched and wait for the next run — seeding
    // here would overwrite the very target we're waiting for. `awaitingDeepLink`
    // makes the page render a skeleton meanwhile, so the operator never sees
    // Wi-Fi flash before the requested tab arrives.
    if (fromHash) { setAwaitingDeepLink(true); return; }
    // No hash at all: seed one for the default first step so the URL reflects the
    // visible tab from the very first render, not just after a click. scrollTo
    // re-selects the current section — a harmless no-op here.
    setAwaitingDeepLink(false);
    scrollTo(visibleSections[0]?.id ?? "wifi");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleSections.length]);

  // Wizard-style step navigation: Prev/Next walk through visibleSections; the
  // submit button only renders on the last visible step. Auto-scroll edge
  // cases (activeSection on a hidden section) fall back to index 0 so Next
  // still advances into the visible set.
  const currentStepIndex = Math.max(0, visibleSections.findIndex((s) => s.id === activeSection));
  const currentStep = visibleSections[currentStepIndex];
  const isFirstStep = currentStepIndex === 0;
  const isLastStep = currentStepIndex >= visibleSections.length - 1;
  // On an optional step the operator hasn't completed yet, the forward action
  // reads "Skip" rather than "Next" so they understand they can move on without
  // enrolling. Once they've enrolled (sectionDone), it reverts to "Next".
  const isSkippableStep = !!currentStep?.optional && !sectionDone[currentStep.id];
  // Overall completion drives the sidebar + topbar progress bars. Counts only
  // the sections currently visible so the denominator matches what the
  // operator actually sees (e.g. ?debug, devicePushedConfig, continue mode).
  const doneCount = visibleSections.filter((s) => sectionDone[s.id]).length;
  const progressPct = visibleSections.length
    ? Math.round((doneCount / visibleSections.length) * 100)
    : 0;
  const goPrev = () => {
    setStepError(null);
    if (isFirstStep) return;
    scrollTo(visibleSections[currentStepIndex - 1].id);
  };
  // Per-step gate: a required step that isn't `sectionDone` blocks Next with an
  // inline, field-specific hint — far better than walking all the way to submit
  // and bouncing back several steps, and clearer than a silently-disabled
  // button (which never explains *why* it won't advance). Optional steps
  // (Voice/Face) always pass. Back never validates.
  const STEP_BLOCK_HINTS: Partial<Record<SectionId, string>> = {
    wifi: "Choose a Wi-Fi network and enter its password before continuing.",
    llm: "Add the AI Brain API key before continuing.",
  };
  const goNext = () => {
    if (isLastStep) return;
    if (currentStep && !currentStep.optional && !sectionDone[currentStep.id]) {
      setStepError(STEP_BLOCK_HINTS[currentStep.id] ?? "Complete this step before continuing.");
      return;
    }
    setStepError(null);
    scrollTo(visibleSections[currentStepIndex + 1].id);
  };

  const uniqueNetworks = useMemo(
    () => [...new Map(networks.filter((n) => n.ssid !== "").map((n) => [n.ssid, n])).values()],
    [networks],
  );

  // Discard the failed attempt entirely and restart the wizard from scratch —
  // the state an operator gets the first time they join the device's AP.
  //
  // This is a HARD RESET by design, not the in-place reset the "Back to Wi-Fi"
  // button performs. A wrong Wi-Fi password leaves the device un-provisioned
  // (device.Setup bails before it writes any config), so nothing about the
  // previous attempt is worth carrying forward — and the operator-supplied
  // params baked into this document at load time (llm_api_key, device_id, …)
  // may themselves be stale, since the companion app reopens the popup with a
  // fresh set. Reloading is what makes them re-read; React state alone cannot,
  // because useSetupUrlParams snapshots them at module scope.
  //
  // Whether the progress/failure screen replaces the form. Either this tab has
  // a join in flight (setupWorking), or it adopted a previous attempt's failure
  // on mount (adoptedFailure).
  //
  // On a device that skips the failure screen, the `failed` phase renders the
  // form instead — the auto-return effect below then clears the underlying
  // state. Suppressing it here as well as in that effect avoids a one-frame
  // flash of the failure screen before the effect runs.
  const showProgressScreen = (setupWorking || adoptedFailure) &&
    !(skipsFailureScreen && setupPhase === "failed");

  // "Back to Wi-Fi" — return to the form after a failure. Clears BOTH paths
  // into the failure screen, otherwise an adopted failure would immediately
  // re-render it and the button would look dead.
  //
  // Wipes every field the failed attempt left behind so the operator lands on
  // the same wizard a first-ever AP visit would give them. This mirrors the
  // adoption path's cleanup (see the mount effect above) field for field — the
  // two are the only routes back to the form, and letting them clear different
  // things would make "retry in this tab" and "reopen after rejoining the AP"
  // behave differently for no reason the operator could predict.
  //
  // Safe to wipe: a Wi-Fi failure bails inside SetupNetwork, before device.Setup
  // writes any config, so none of this was ever persisted device-side. The
  // password in particular must go — keeping it lets the operator resubmit the
  // exact value that just failed without noticing. The SSID goes with it so the
  // picker re-resolves from the fresh scan (and from useConfigPrefill /
  // useWifiConnected) instead of pinning a stale choice.
  //
  // NOT a reload: this keeps the current document — and the params the parent
  // pushed into it (llm_api_key, channel, device_id) — alive, so the operator
  // only re-enters Wi-Fi credentials rather than the whole setup. That was the
  // "Start over" button's job, and it is deliberately gone.
  //
  // Nothing to clear server-side: the device never wrote config for a failed
  // join. Its in-memory setupState still reports phase="failed" (no code path
  // resets it to idle — see system/device/setup.go), which is exactly why the
  // poll ignores terminal verdicts until it has seen phase="connecting" confirm
  // the new run started. Without that guard this reset would be undone within
  // 600ms by the previous attempt's verdict.
  const retryFromFailure = useCallback(() => {
    setupBridge.retryClicked();
    setSetupWorking(false);
    setAdoptedFailure(false);
    setSetupPhase("connecting");
    setSetupErrorMsg("");
    setSetupLanIP("");
    setElapsed(0);
    setSsid("");
    setPassword("");
    setAdminPassword("");
    setError(null);
    setStepError(null);
    setActiveSection("wifi");
    // sessionStorage survives F5, so an operator who reloads this tab after a
    // failure would otherwise rehydrate the failed attempt's query string —
    // a form that looks clean but still ships the old llm_api_key.
    clearStoredSetupParams();
  }, []);

  // Devices in `skipsFailureScreen`: a failed join sends the operator straight
  // back to the Wi-Fi form instead of showing the failure screen — exactly as
  // if they had pressed "Back to Wi-Fi" themselves. Covers both routes into a
  // failure: this tab's own join timing out, and a previous attempt's verdict
  // adopted on mount.
  //
  // retryFromFailure() is reused verbatim so the two paths can't drift: it
  // clears setupWorking/adoptedFailure, resets the phase, drops the password
  // that failed and returns to the Wi-Fi step. It also emits `retry_clicked`
  // on the bridge — accurate, since the outcome is identical to the operator
  // pressing the button; the parent still receives `setup_failed` first, so it
  // can tell a join failed even though no screen was shown.
  //
  // Per product decision this leaves NO on-screen trace of the failure: the
  // form's error banner is not populated, so the operator simply finds
  // themselves back at Wi-Fi entry.
  useEffect(() => {
    if (!skipsFailureScreen) return;
    if (setupPhase !== "failed") return;
    if (!setupWorking && !adoptedFailure) return;
    retryFromFailure();
  }, [skipsFailureScreen, setupPhase, setupWorking, adoptedFailure, retryFromFailure]);

  // Elapsed-seconds ticker for the connecting screen. Runs only while we're in
  // the connecting phase; resets on entry so re-tries (failed → Back → submit)
  // start from zero. Cleared on unmount / phase change.
  useEffect(() => {
    if (!setupWorking || setupPhase !== "connecting") return;
    setElapsed(0);
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [setupWorking, setupPhase]);

  // Join-progress heartbeat → parent window. Fire setupBridge.joinProgress once
  // when the join has been running ~4s without flipping to connected/failed, so
  // the opener (autonomous.ai) learns the join is taking a moment but is still
  // alive. joinPingedRef guards single-fire; it resets whenever we (re)enter the
  // connecting phase so a retry pings again.
  const joinPingedRef = useRef(false);
  useEffect(() => {
    if (setupPhase === "connecting" && setupWorking) {
      if (elapsed >= 4 && !joinPingedRef.current) {
        joinPingedRef.current = true;
        setupBridge.joinProgress(elapsed);
      }
    } else {
      // Left the connecting phase (connected/failed or screen torn down) — arm
      // the heartbeat again for the next connecting session.
      joinPingedRef.current = false;
    }
  }, [elapsed, setupPhase, setupWorking]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Admin password is no longer collected up front: the backend
    // (handler.Setup) defaults an empty AdminPassword to the device's
    // hardware suffix (last 4 chars after '-' in GetDeviceMac()), and the
    // operator reads that suffix from the sticker at the bottom of the
    // device — see Login page hint.
    //
    // Pre-flight check for the two visible Wi-Fi fields. Catches implicit
    // Enter-key form submission and any other accidental fire-before-ready
    // path with a plain hint instead of letting the Go validator return a
    // tag-format error. Other required fields (LLM creds and device ID) ride
    // through URL params or the saved config merge on the
    // backend, so we let the server be the source of truth for those — see
    // the normaliseSetupError() catch below for friendlier rendering.
    // No network chosen is only an error when the device actually needs one.
    // On a wired device we submit an empty ssid on purpose — the backend reads
    // that as "keep using the uplink you already have", verifies it, and tears
    // down the provisioning AP instead of running a Wi-Fi join.
    if (!ssid.trim()) {
      if (!wiredUplink) {
        setError("Choose a Wi-Fi network before continuing.");
        setActiveSection("wifi");
        return;
      }
    } else if (!password && !hasNetworkPassword) {
      setError("Enter the Wi-Fi password.");
      setActiveSection("wifi");
      return;
    }
    setLoading(true);
    try {
      let channelCredentials: Record<string, string> = {};
      switch (channel) {
        case "telegram":
          channelCredentials = {
            telegram_bot_token: urlParams.teleToken || teleToken,
            telegram_user_id: urlParams.teleUserId || teleUserId,
          };
          break;
        case "slack":
          channelCredentials = {
            slack_bot_token: urlParams.slackBotToken || slackBotToken,
            slack_app_token: urlParams.slackAppToken || slackAppToken,
            slack_user_id: urlParams.slackUserId || slackUserId,
          };
          break;
        case "discord":
          channelCredentials = {
            discord_bot_token: urlParams.discordBotToken || discordBotToken,
            discord_guild_id: urlParams.discordGuildId || discordGuildId,
            discord_user_id: urlParams.discordUserId || discordUserId,
          };
          break;
      }
      const body: Parameters<typeof setupDevice>[0] = {
        ssid: ssid.trim(), password,
        ...(channel === "none" ? {} : { channel }),
        ...channelCredentials,
        llm_base_url: urlParams.llmUrl || llmUrl,
        llm_api_key: urlParams.llmApiKey || llmApiKey,
        llm_model: urlParams.llmModel || llmModel,
        llm_disable_thinking: llmDisableThinking || undefined,
        deepgram_api_key: urlParams.deepgramApiKey || undefined,
        stt_api_key: sttApiKey || undefined,
        stt_base_url: sttBaseUrl || undefined,
        stt_language: sttLanguage || undefined,
        tts_api_key: ttsApiKey || undefined,
        tts_base_url: ttsBaseUrl || undefined,
        tts_provider: ttsProvider || undefined,
        tts_voice: ttsVoice || undefined,
        device_id: urlParams.deviceId || deviceId,
        admin_password: adminPassword || undefined,
      };
      const endpoint = mqttEndpoint || urlParams.mqttEndpoint;
      if (endpoint) {
        const port = mqttPort || urlParams.mqttPort;
        Object.assign(body, {
          mqtt_endpoint: endpoint,
          mqtt_port: port ? parseInt(port, 10) : 1883,
          mqtt_username: mqttUsername || urlParams.mqttUsername || undefined,
          mqtt_password: mqttPassword || urlParams.mqttPassword || undefined,
          fa_channel: faChannel || urlParams.faChannel || undefined,
          fd_channel: fdChannel || urlParams.fdChannel || undefined,
        });
      }
      setupBridge.submitted({ ssid: ssid.trim(), channel });
      // Switch to the "Setting up…" screen and START the status poller BEFORE
      // awaiting the (blocking) setup POST. This is critical on single-radio
      // devices: POST /api/device/setup blocks while wlan0 switches AP→STA, and
      // the AP at 192.168.100.1 tears down ~2s into that switch — which aborts
      // the in-flight POST and kills any same-origin request. The poller has to
      // already be running during that brief AP-alive window to read back the
      // device's new LAN IP (lan_ip) from setup/status; if we waited for the
      // POST to resolve first, the AP would be dead and every poll would fail,
      // stranding the user on "joining Wi-Fi…" forever (the bug we're fixing).
      setSetupWorking(true);
      setWiredRun(!ssid.trim());
      setSetupPhase("connecting");
      try {
        await setupDevice(body);
      } catch {
        // Expected: the AP drops mid-request once the STA association starts, so
        // the POST often rejects with a network error even on a SUCCESSFUL
        // setup. Swallow it and let the poller + LAN-IP probe drive the rest —
        // they're the source of truth for connected/failed now. A genuine
        // validation failure would have rejected before the AP teardown; that's
        // surfaced by the status poller reporting phase="failed".
      }
    } catch (err) {
      // Only reaches here for synchronous/pre-POST errors (e.g. building the
      // body). Real submit failures flow through the inner catch + poller.
      setSetupWorking(false);
      setError(normaliseSetupError(err instanceof Error ? err.message : "Setup failed."));
    }
    setLoading(false);
  }, [
    channel, urlParams, teleToken, teleUserId, slackBotToken, slackAppToken, slackUserId,
    discordBotToken, discordGuildId, discordUserId, ssid, password, llmUrl, llmApiKey,
    llmModel, llmDisableThinking, sttApiKey, sttBaseUrl, ttsApiKey, ttsBaseUrl, ttsVoice, deviceId,
    mqttEndpoint, mqttPort, mqttUsername, mqttPassword, faChannel, fdChannel,
    sttLanguage, ttsProvider, hasNetworkPassword, adminPassword, wiredUplink,
  ]);

  return {
    // theme / chrome
    theme, toggleTheme, themeClass,
    // layout + navigation
    contentRef, visibleSections, activeSection, scrollTo,
    currentStepIndex, isFirstStep, isLastStep, isSkippableStep,
    doneCount, progressPct, sectionDone, goPrev, goNext,
    // mode flags
    isContinue, devicePushedConfig, awaitingDeepLink,
    // post-submit screen
    setupWorking, showProgressScreen, setupPhase, setupLanIP, setupErrorMsg, elapsed, wiredRun,
    setSetupWorking, setSetupPhase, setActiveSection,
    deviceMdnsHost, deviceTypePrefix, retryFromFailure, finishWizard,
    // form-level status
    error, stepError, loading, loadingList,
    handleSubmit, navigate,
    // Wi-Fi
    ssid, setSsid, password, setPassword,
    hasAdminPassword, hasNetworkPassword,
    adminPassword, setAdminPassword,
    uniqueNetworks, refreshNetworks, wifiConnected, wiredUplink, currentSsid, wifiChecking,
    // LLM
    llmLoaded, llmApiKey, setLlmApiKey, llmUrl, setLlmUrl, llmModel, setLlmModel,
    // channel
    channel, setChannel, channelLoaded,
    teleToken, setTeleToken, teleUserId, setTeleUserId,
    slackBotToken, setSlackBotToken, slackAppToken, setSlackAppToken, slackUserId, setSlackUserId,
    discordBotToken, setDiscordBotToken, discordGuildId, setDiscordGuildId, discordUserId, setDiscordUserId,
    // iMessage caller-context prompt (optional, non-secret plaintext).
    // Returned alongside the other channel state so a future Setup-wizard
    // ChannelSection can render the textarea; hydrated by useConfigPrefill.
    bluebubblesCallerContext, setBluebubblesCallerContext,
    // language + TTS
    sttLanguage, setSttLanguage,
    ttsProvider, setTtsProvider, ttsProviders, ttsVoice, setTtsVoice, ttsVoices,
    // enrollment
    faceOwners, loadFaceOwners, canEnrollVoice, canEnrollFace,
  };
}
