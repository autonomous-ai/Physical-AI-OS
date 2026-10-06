import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useSearchParams, useNavigate } from "react-router-dom";
import { getNetworks, getSetupStatus, setupDevice } from "@/lib/api";
import { useTheme } from "@/lib/useTheme";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useSetupUrlParams, clearStoredSetupParams } from "@/hooks/setup/useSetupUrlParams";
import { useTTSCatalog } from "@/hooks/setup/useTTSCatalog";
import { useConfigPrefill } from "@/hooks/setup/useConfigPrefill";
import { useSetupStatusPolling, type SetupPhase } from "@/hooks/setup/useSetupStatusPolling";
import { useFaceEnroll } from "@/hooks/setup/useFaceEnroll";
import { useWifiConnected } from "@/hooks/setup/useWifiConnected";
import { useCapabilities } from "@/hooks/useCapabilities";
import { Cap } from "@/pages/monitor/types";
import { setupBridge } from "@/lib/setupBridge";
import type { SectionId, LlmLoadedState, ChannelLoadedState } from "@/hooks/setup/types";
import type { ChannelType, NetworkItem, SetupChannelType } from "@/types";
import { normaliseSetupError, type SetupMode } from "./helpers";

// Owns all Setup page state, effects and handlers; the Setup component only renders.
export function useSetupController(mode: SetupMode) {
  // #force forces initial mode for UI testing, but feature gates treat it as continue.
  const forceHash = typeof window !== "undefined" && window.location.hash === "#force";
  const isContinue = mode === "continue" || forceHash;
  // Local dev hosts skip the auto-bounce to /monitor.
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

  // llm_api_key in the URL means the OS server pushed a full config; only Wi-Fi is shown.
  const devicePushedConfig = mode === "initial" && !!urlParams.llmApiKey;

  const debug = searchParams.get("debug") === "true";

  const [networks, setNetworks] = useState<NetworkItem[]>([]);
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingList, setLoadingList] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stepError, setStepError] = useState<string | null>(null);
  const [setupWorking, setSetupWorking] = useState(false);
  // Latched at submit: this run carried no SSID (wired branch).
  const [wiredRun, setWiredRun] = useState(false);
  // Adopted a previous attempt's failure; separate from setupWorking, which drives the live pollers.
  const [adoptedFailure, setAdoptedFailure] = useState(false);
  const [setupPhase, setSetupPhase] = useState<SetupPhase>("connecting");
  const [setupLanIP, setSetupLanIP] = useState<string>("");
  const [setupErrorMsg, setSetupErrorMsg] = useState<string>("");
  const [elapsed, setElapsed] = useState(0);
  const [activeSection, setActiveSection] = useState<SectionId>("wifi");
  const contentRef = useRef<HTMLDivElement>(null);

  const [deviceId, setDeviceId] = useState(urlParams.deviceId || "");
  const [mac, setMac] = useState("");
  // Hidden input; submitted empty, the backend defaults it to the hardware suffix.
  const [adminPassword, setAdminPassword] = useState("");
  // Starts true to avoid flashing the admin-password fields during the probe.
  const [hasAdminPassword, setHasAdminPassword] = useState(true);
  const [hasNetworkPassword, setHasNetworkPassword] = useState(false);
  // `<device_type>-<suffix>.local`, derived like system/device/hardware.go.
  const deviceMdnsHost = useMemo(() => {
    const host = (mac || "").trim().toLowerCase();
    if (!/^[0-9a-f]{4}$/.test(host.slice(-4))) return "";
    return host;
  }, [mac]);
  const deviceTypePrefix = useMemo(() => {
    const m = (mac || "").trim().toLowerCase();
    const dash = m.lastIndexOf("-");
    return dash > 0 ? m.slice(0, dash + 1) : "";
  }, [mac]);
  // The class may contain dashes, hence lastIndexOf("-").
  const deviceType = useMemo(
    () => deviceTypePrefix.replace(/-$/, ""),
    [deviceTypePrefix],
  );
  // Product decision: these devices skip the failure screen.
  const skipsFailureScreen = deviceType === "intern-v2";
  const [llmApiKey, setLlmApiKey] = useState(urlParams.llmApiKey || "");
  const [llmUrl, setLlmUrl] = useState(urlParams.llmUrl || "");
  const [llmModel, setLlmModel] = useState(urlParams.llmModel || "");
  const [llmLoaded, setLlmLoaded] = useState<LlmLoadedState>({
    apiKey: !!urlParams.llmApiKey,
    baseUrl: !!urlParams.llmUrl,
    model: !!urlParams.llmModel,
  });
  const [llmDisableThinking, setLlmDisableThinking] = useState(false);
  const [ttsApiKey, setTtsApiKey] = useState(urlParams.ttsApiKey || "");
  const [ttsBaseUrl, setTtsBaseUrl] = useState(urlParams.ttsBaseUrl || "");
  const [sttApiKey, setSttApiKey] = useState("");
  const [sttBaseUrl, setSttBaseUrl] = useState("");
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
  const [bluebubblesCallerContext, setBluebubblesCallerContext] = useState("");
  const [channelLoaded, setChannelLoaded] = useState<ChannelLoadedState>({
    teleToken: !!urlParams.teleToken, teleUserId: !!urlParams.teleUserId,
    slackBotToken: !!urlParams.slackBotToken, slackAppToken: !!urlParams.slackAppToken,
    slackUserId: !!urlParams.slackUserId,
    discordBotToken: !!urlParams.discordBotToken, discordGuildId: !!urlParams.discordGuildId,
    discordUserId: !!urlParams.discordUserId,
    bluebubblesServerUrl: false, bluebubblesPassword: false, bluebubblesUserAddress: false,
    bluebubblesCallerContext: false,
  });
  const [mqttEndpoint, setMqttEndpoint] = useState("");
  const [mqttPort, setMqttPort] = useState("");
  const [mqttUsername, setMqttUsername] = useState("");
  const [mqttPassword, setMqttPassword] = useState("");
  const [faChannel, setFaChannel] = useState("");
  const [fdChannel, setFdChannel] = useState("");

  const { faceOwners, loadFaceOwners } = useFaceEnroll();

  // Wi-Fi state from the live device, not form fields (they reset on the AP->STA reload).
  const { wifiConnected, wiredUplink, currentSsid, checking: wifiChecking } = useWifiConnected();

  // Enrollment steps require the matching capability (mic/camera); fail-open while loading.
  const { hasCap } = useCapabilities();
  const canEnrollVoice = hasCap(Cap.Audio);
  const canEnrollFace = hasCap(Cap.Vision);

  // Saved secrets count via their *Loaded presence flags.
  const sectionDone: Record<SectionId, boolean> = {
    device: true,
    // wifiConnected/wiredUplink short-circuit this after the AP->STA reload.
    wifi: wifiConnected || wiredUplink || (!!ssid && (!!password || hasNetworkPassword)),
    llm: !!llmApiKey || llmLoaded.apiKey,
    language: true,
    realtime: true,
    channel: true,
    tts: !!ttsVoice,
    voice: faceOwners.some((p) => (p.voice_samples?.length ?? 0) > 0),
    face: faceOwners.some((p) => p.photo_count > 0),
    deepgram: true,
    mqtt: true,
    stt: true,
  };

  useEffect(() => {
    setMqttEndpoint((prev) => prev || urlParams.mqttEndpoint);
    setMqttPort((prev) => prev || urlParams.mqttPort);
    setMqttUsername((prev) => prev || urlParams.mqttUsername);
    setMqttPassword((prev) => prev || urlParams.mqttPassword);
    setFaChannel((prev) => prev || urlParams.faChannel);
    setFdChannel((prev) => prev || urlParams.fdChannel);
  }, [urlParams]);

  useEffect(() => {
    if (currentSsid) setSsid((prev) => prev || currentSsid);
  }, [currentSsid]);

  useEffect(() => {
    if (isContinue) loadFaceOwners();
  }, [isContinue, loadFaceOwners]);

  // Continue mode: scroll to the first incomplete section, or bounce to /monitor when all are done.
  const autoScrolledRef = useRef(false);
  // `setup_done` must fire on every route out of a finished wizard.
  const setupDoneRef = useRef(false);
  const finishWizard = useCallback(() => {
    if (setupDoneRef.current) return;
    setupDoneRef.current = true;
    setupBridge.setupDone();
  }, []);
  // Set when the URL hash deep-linked a visible step; auto-scroll must not override it.
  const deepLinkedRef = useRef(false);
  // True while the hash names a step that is not visible yet (mode resolving).
  const [awaitingDeepLink, setAwaitingDeepLink] = useState(() => {
    if (typeof window === "undefined") return false;
    const h = window.location.hash.replace(/^#/, "");
    return !!h && h !== "force" && h !== "wifi";
  });
  useEffect(() => {
    if (!isContinue) return;
    if (!llmApiKey) return;
    // Wait for the live network probe; otherwise Wi-Fi reads as incomplete and pins the wizard there.
    if (wifiChecking) return;
    const required: SectionId[] = ["wifi", "llm", "tts",
      ...(canEnrollVoice ? ["voice" as SectionId] : []),
      ...(canEnrollFace ? ["face" as SectionId] : []),
    ];
    // Not gated by autoScrolledRef: async data can complete the set later.
    if (required.every((id) => sectionDone[id])) {
      if (autoScrolledRef.current && !forceHash && !isLocalDev) {
        finishWizard();
        navigate("/monitor", { replace: true });
      }
      return;
    }
    if (autoScrolledRef.current) return;
    if (deepLinkedRef.current) { autoScrolledRef.current = true; return; }
    const order: SectionId[] = ["wifi", "llm", "channel", "language", "tts",
      ...(canEnrollVoice ? ["voice" as SectionId] : []),
      ...(canEnrollFace ? ["face" as SectionId] : []),
    ];
    const next = order.find((id) => !sectionDone[id]) ?? "tts";
    setActiveSection(next);
    autoScrolledRef.current = true;
    // forceHash/isLocalDev are read off window.location, not state; listing them would re-enter this navigation effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isContinue, llmApiKey, wifiChecking, canEnrollVoice, canEnrollFace, sectionDone, navigate, finishWizard]);

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

  // No-op while a scan is in flight (parallel `iw scan` calls fight for the radio).
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

  // Recover a failed join this tab never witnessed (the AP died before the verdict).
  const failureAdoptedRef = useRef(false);
  useEffect(() => {
    if (failureAdoptedRef.current) return;
    if (setupWorking) return;
    // Continue mode: a stale `failed` verdict cannot describe this online device.
    if (isContinue) return;
    let cancelled = false;
    getSetupStatus().then((s) => {
      if (cancelled || s.phase !== "failed") return;
      failureAdoptedRef.current = true;
      // Decide from THIS response's mac rather than the `mac` state, which is still empty on mount (useConfigPrefill fills it from a separate request).
      const macType = (s.mac || "").trim().toLowerCase().replace(/-[0-9a-f]{4}$/, "");
      const skipScreen = macType === "intern-v2";
      // Wipe the failed attempt's fields; nothing was persisted device-side.
      setSsid("");
      setPassword("");
      setAdminPassword("");
      setError(null);
      setStepError(null);
      setSetupLanIP("");
      setActiveSection("wifi");
      // sessionStorage survives F5; drop the failed attempt's params.
      clearStoredSetupParams();
      if (skipScreen) {
        setupBridge.failed(s.error || "Wi-Fi setup failed.");
        return;
      }
      setSetupErrorMsg(s.error || "Wi-Fi setup failed.");
      setSetupPhase("failed");
      // adoptedFailure, not setupWorking: restarting the poll would re-assert the stale verdict in a loop.
      setAdoptedFailure(true);
    }).catch(() => {
      /* Status unreachable: leave the form as-is. */
    });
    return () => { cancelled = true; };
    // Mount-only, like the other setup hooks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Parent-window bridge: best-effort postMessage milestones (lib/setupBridge.ts).
  useEffect(() => {
    setupBridge.opened({ mode, deviceId, mac });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { setupBridge.stepChanged(activeSection); }, [activeSection]);
  useEffect(() => { if (ssid) setupBridge.wifiSelected(ssid); }, [ssid]);
  useEffect(() => { if (error) setupBridge.error(error); }, [error]);
  useEffect(() => {
    if (!setupWorking) return;
    if (setupPhase === "connecting") setupBridge.connecting();
    else if (setupPhase === "connected") setupBridge.connected({ mdns_host: deviceMdnsHost, lan_ip: setupLanIP });
    else if (setupPhase === "failed") setupBridge.failed(setupErrorMsg || "Wi-Fi setup failed.");
  }, [setupWorking, setupPhase, deviceMdnsHost, setupLanIP, setupErrorMsg]);


  // Mirror AI Brain key/URL into TTS while the TTS field is empty.
  useEffect(() => {
    if (!ttsApiKey && llmApiKey) setTtsApiKey(llmApiKey);
  }, [llmApiKey, ttsApiKey]);
  useEffect(() => {
    if (!ttsBaseUrl && llmUrl) setTtsBaseUrl(llmUrl);
  }, [llmUrl, ttsBaseUrl]);
  useEffect(() => {
    if (!sttApiKey && llmApiKey) setSttApiKey(llmApiKey);
  }, [llmApiKey, sttApiKey]);
  useEffect(() => {
    if (!sttBaseUrl && llmUrl) setSttBaseUrl(llmUrl);
  }, [llmUrl, sttBaseUrl]);

  const scrollTo = (id: SectionId) => {
    setActiveSection(id);
    setStepError(null);
    // Mirror the active step into the URL hash, preserving #force.
    if (typeof window !== "undefined" && window.location.hash !== "#force") {
      // replaceState so Back exits Setup instead of walking tabs.
      history.replaceState(null, "", `${window.location.pathname}${window.location.search}#${id}`);
    }
    contentRef.current?.scrollTo({ top: 0 });
  };

  const SECTIONS: { id: SectionId; label: string; optional?: boolean }[] = [
    { id: "wifi", label: "Wi-Fi" },
    ...(debug ? [
      { id: "llm" as SectionId, label: "AI Brain" },
      { id: "channel" as SectionId, label: "Channels", optional: true },
      { id: "language" as SectionId, label: "Language" },
      { id: "tts" as SectionId, label: "Voice" },
    ] : []),
    ...(isContinue && canEnrollVoice ? [
      { id: "voice" as SectionId, label: "My Voice", optional: true },
    ] : []),
    ...(isContinue && canEnrollFace ? [
      { id: "face" as SectionId, label: "Face", optional: true },
    ] : []),
  ];

  const visibleSections = devicePushedConfig
    ? SECTIONS.filter((s) => s.id === "wifi")
    : SECTIONS;

  // Deep-link: open the visible step named by the hash; re-runs as sections appear.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (deepLinkedRef.current) return;
    const fromHash = window.location.hash.replace(/^#/, "");
    if (fromHash === "force") return;
    if (fromHash && visibleSections.some((s) => s.id === fromHash)) {
      deepLinkedRef.current = true;
      setAwaitingDeepLink(false);
      scrollTo(fromHash as SectionId);
      return;
    }
    // Hash names a step not visible yet: wait (the skeleton renders meanwhile).
    if (fromHash) { setAwaitingDeepLink(true); return; }
    setAwaitingDeepLink(false);
    scrollTo(visibleSections[0]?.id ?? "wifi");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleSections.length]);

  const currentStepIndex = Math.max(0, visibleSections.findIndex((s) => s.id === activeSection));
  const currentStep = visibleSections[currentStepIndex];
  const isFirstStep = currentStepIndex === 0;
  const isLastStep = currentStepIndex >= visibleSections.length - 1;
  const isSkippableStep = !!currentStep?.optional && !sectionDone[currentStep.id];
  const doneCount = visibleSections.filter((s) => sectionDone[s.id]).length;
  const progressPct = visibleSections.length
    ? Math.round((doneCount / visibleSections.length) * 100)
    : 0;
  const goPrev = () => {
    setStepError(null);
    if (isFirstStep) return;
    scrollTo(visibleSections[currentStepIndex - 1].id);
  };
  // Per-step gate: a required incomplete step blocks Next with a hint.
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

  // The progress/failure screen replaces the form while a join is in flight or a prior failure was adopted.
  const showProgressScreen = (setupWorking || adoptedFailure) &&
    !(skipsFailureScreen && setupPhase === "failed");

  // "Back to Wi-Fi": clear both failure paths and wipe the attempt's fields.
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
    // sessionStorage survives F5; drop the failed attempt's params.
    clearStoredSetupParams();
  }, []);

  // skipsFailureScreen devices go straight back to the Wi-Fi form.
  useEffect(() => {
    if (!skipsFailureScreen) return;
    if (setupPhase !== "failed") return;
    if (!setupWorking && !adoptedFailure) return;
    retryFromFailure();
  }, [skipsFailureScreen, setupPhase, setupWorking, adoptedFailure, retryFromFailure]);

  useEffect(() => {
    if (!setupWorking || setupPhase !== "connecting") return;
    setElapsed(0);
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [setupWorking, setupPhase]);

  // Fire setupBridge.joinProgress once after ~4s of joining.
  const joinPingedRef = useRef(false);
  useEffect(() => {
    if (setupPhase === "connecting" && setupWorking) {
      if (elapsed >= 4 && !joinPingedRef.current) {
        joinPingedRef.current = true;
        setupBridge.joinProgress(elapsed);
      }
    } else {
      joinPingedRef.current = false;
    }
  }, [elapsed, setupPhase, setupWorking]);

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    // Pre-flight check for the visible Wi-Fi fields; a wired device may submit an empty SSID on purpose.
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
      // Start the poller BEFORE the blocking POST: the AP dies ~2s into the switch, taking the POST with it.
      setSetupWorking(true);
      setWiredRun(!ssid.trim());
      setSetupPhase("connecting");
      try {
        await setupDevice(body);
      } catch {
        // Expected: the AP drops mid-request even on success; the poller decides.
      }
    } catch (err) {
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
    theme, toggleTheme, themeClass,
    contentRef, visibleSections, activeSection, scrollTo,
    currentStepIndex, isFirstStep, isLastStep, isSkippableStep,
    doneCount, progressPct, sectionDone, goPrev, goNext,
    isContinue, devicePushedConfig, awaitingDeepLink,
    setupWorking, showProgressScreen, setupPhase, setupLanIP, setupErrorMsg, elapsed, wiredRun,
    setSetupWorking, setSetupPhase, setActiveSection,
    deviceMdnsHost, deviceTypePrefix, retryFromFailure, finishWizard,
    error, stepError, loading, loadingList,
    handleSubmit, navigate,
    ssid, setSsid, password, setPassword,
    hasAdminPassword, hasNetworkPassword,
    adminPassword, setAdminPassword,
    uniqueNetworks, refreshNetworks, wifiConnected, wiredUplink, currentSsid, wifiChecking,
    llmLoaded, llmApiKey, setLlmApiKey, llmUrl, setLlmUrl, llmModel, setLlmModel,
    channel, setChannel, channelLoaded,
    teleToken, setTeleToken, teleUserId, setTeleUserId,
    slackBotToken, setSlackBotToken, slackAppToken, setSlackAppToken, slackUserId, setSlackUserId,
    discordBotToken, setDiscordBotToken, discordGuildId, setDiscordGuildId, discordUserId, setDiscordUserId,
    bluebubblesCallerContext, setBluebubblesCallerContext,
    sttLanguage, setSttLanguage,
    ttsProvider, setTtsProvider, ttsProviders, ttsVoice, setTtsVoice, ttsVoices,
    faceOwners, loadFaceOwners, canEnrollVoice, canEnrollFace,
  };
}
