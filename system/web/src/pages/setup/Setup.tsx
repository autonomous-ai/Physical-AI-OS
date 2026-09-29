import { setupBridge } from "@/lib/setupBridge";
import type { SectionId } from "@/hooks/setup/types";
import { C } from "@/components/setup/shared";
import { WifiSection } from "@/components/setup/WifiSection";
import { LLMSection } from "@/components/setup/LLMSection";
import { ChannelSection } from "@/components/setup/ChannelSection";
import { LanguageSection } from "@/components/setup/LanguageSection";
import { TTSSection } from "@/components/setup/TTSSection";
import { VoiceSection } from "@/pages/settings/VoiceSection";
import { FaceSection } from "@/pages/settings/FaceSection";
import { Wifi, Brain, Volume2, MessageSquare, UserCircle, Mic, Globe, Check } from "lucide-react";
import type { SetupMode } from "./helpers";
import { useSetupController } from "./useSetupController";
import { SetupProgressScreen } from "./SetupProgressScreen";
import { SetupSkeleton } from "./SetupSkeleton";

const SECTION_ICONS: Partial<Record<SectionId, React.ReactNode>> = {
  wifi: <Wifi size={15} />,
  llm: <Brain size={15} />,
  channel: <MessageSquare size={15} />,
  language: <Globe size={15} />,
  tts: <Volume2 size={15} />,
  voice: <Mic size={15} />,
  face: <UserCircle size={15} />,
};

interface SetupProps {
  mode?: SetupMode;
}

// Thin view: all state, effects and handlers live in useSetupController.
export default function Setup({ mode = "initial" }: SetupProps = {}) {
  const {
    theme, toggleTheme, themeClass,
    contentRef, visibleSections, activeSection, scrollTo,
    currentStepIndex, isFirstStep, isLastStep, isSkippableStep,
    doneCount, progressPct, sectionDone, goPrev, goNext,
    isContinue, devicePushedConfig, awaitingDeepLink,
    showProgressScreen, setupPhase, setupLanIP, setupErrorMsg, elapsed, wiredRun,
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
    sttLanguage, setSttLanguage,
    ttsProvider, setTtsProvider, ttsProviders, ttsVoice, setTtsVoice, ttsVoices,
    faceOwners, loadFaceOwners, canEnrollVoice, canEnrollFace,
  } = useSetupController(mode);

  // Hold the skeleton while a deep-linked step is not visible yet.
  if (awaitingDeepLink && !showProgressScreen) return <SetupSkeleton />;

  return (
    <div className={`lm-root lm-setup ${themeClass}`} style={{
      display: "flex", height: "100vh",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
    }}>
      <aside className="lm-sidebar" style={{
        width: 192, flexShrink: 0,
        background: C.sidebar, borderRight: `1px solid ${C.border}`,
        display: "flex", flexDirection: "column",
      }}>

        <div style={{ padding: "16px 16px 12px" }}>
          <div style={{ fontSize: 14.5, fontWeight: 700, color: C.text, letterSpacing: "0.01em" }}>
            Robot Setup
          </div>
          <div style={{ fontSize: 12, color: C.textMuted, marginTop: 3 }}>
            {doneCount} of {visibleSections.length} done
          </div>
          <div className="lm-progress-track" style={{ marginTop: 10 }}>
            <div className="lm-progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
        </div>

        <nav style={{ padding: "4px 0 10px", flex: 1 }}>
          {visibleSections.map((s) => {
            const active = activeSection === s.id;
            const done = sectionDone[s.id];
            return (
              <button
                key={s.id}
                onClick={() => scrollTo(s.id)}
                className={`lm-nav-item${active ? " lm-nav-item--active" : ""}${done && !active ? " lm-nav-item--done" : ""}`}
              >
                {SECTION_ICONS[s.id]}
                <span className="lm-nav-label" style={{ flex: 1 }}>{s.label}</span>
                {s.optional && !done && (
                  <span className="lm-nav-badge" style={{
                    fontSize: 10, fontWeight: 600, color: C.textMuted,
                    textTransform: "uppercase", letterSpacing: "0.04em",
                  }}>
                    Optional
                  </span>
                )}
                {done && <Check size={14} className="lm-pop" style={{ color: C.green }} />}
              </button>
            );
          })}
        </nav>

        <div style={{ padding: "12px 16px", borderTop: `1px solid ${C.border}`, display: "flex", alignItems: "center", justifyContent: "flex-end" }}>
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 4px",
          }} title={`Theme: ${theme}`}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>
      </aside>

      <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {visibleSections.length > 1 && (
        <div className="lm-mobile-tabs-wrap" style={{
          display: "none", flexShrink: 0,
          borderBottom: `1px solid ${C.border}`,
          alignItems: "center", gap: 4, padding: "8px 8px 8px 12px",
        }}>
          <div className="lm-mobile-tabs lm-hide-scroll" style={{
            display: "flex", overflowX: "auto", gap: 4, flex: 1, alignItems: "center",
          }}>
            {visibleSections.map((s) => {
              const active = activeSection === s.id;
              return (
                <button
                  key={s.id}
                  onClick={() => scrollTo(s.id)}
                  className={`lm-tab${active ? " lm-tab--active" : ""}`}
                >
                  {s.label}
                </button>
              );
            })}
          </div>
          <button onClick={toggleTheme} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 14, color: C.textMuted, padding: "2px 6px", flexShrink: 0,
          }}>
            {theme === "dark" ? "◑" : "◐"}
          </button>
        </div>
        )}

        <div style={{ borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
          <div style={{
            padding: "12px 24px 10px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <span style={{ fontSize: 15, fontWeight: 600, color: C.text }}>
              {showProgressScreen ? "Setting up…" : visibleSections.find((s) => s.id === activeSection)?.label ?? "Wi-Fi"}
            </span>
            {!showProgressScreen && visibleSections.length > 1 && (
              <span style={{ fontSize: 12, color: C.textDim }}>
                Step {currentStepIndex + 1} / {visibleSections.length}
              </span>
            )}
          </div>
          {!showProgressScreen && visibleSections.length > 1 && (
            <div className="lm-progress-track" style={{ borderRadius: 0 }}>
              <div
                className="lm-progress-fill"
                style={{
                  borderRadius: 0,
                  width: `${((currentStepIndex + 1) / Math.max(1, visibleSections.length)) * 100}%`,
                }}
              />
            </div>
          )}
        </div>

        <div ref={contentRef} className="lm-fade-in lm-main-content" style={{
          flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px",
        }}>
          <div style={{ maxWidth: 560, margin: "0 auto" }}>

            {showProgressScreen ? (
              <SetupProgressScreen
                setupPhase={setupPhase}
                setupLanIP={setupLanIP}
                setupErrorMsg={setupErrorMsg}
                elapsed={elapsed}
                deviceMdnsHost={deviceMdnsHost}
                deviceTypePrefix={deviceTypePrefix}
                wired={wiredRun}
                onRetry={retryFromFailure}
              />
            ) : (
              <>
                {error && (
                  <div className="lm-fade-in" style={{
                    background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)",
                    borderRadius: 8, padding: "10px 14px", fontSize: 12, color: C.red, marginBottom: 16,
                  }}>
                    {error}
                  </div>
                )}

                <form id="setup-form" onSubmit={handleSubmit} noValidate>

                  <WifiSection
                    active={activeSection === "wifi"}
                    ssid={ssid} setSsid={setSsid}
                    password={password} setPassword={setPassword}
                    passwordConfigured={hasNetworkPassword && !password}
                    connectedSsid={wifiConnected ? currentSsid : ""}
                    checkingConnection={wifiChecking}
                    wiredUplink={wiredUplink}
                    loadingList={loadingList}
                    uniqueNetworks={uniqueNetworks}
                    refreshNetworks={refreshNetworks}
                    {...(!hasAdminPassword ? {
                      adminPassword: adminPassword,
                      setAdminPassword: setAdminPassword,
                    } : {})}
                  />

                  <div style={devicePushedConfig ? { display: "none" } : undefined}>
                    <LLMSection
                      active={devicePushedConfig || activeSection === "llm"}
                      llmLoaded={llmLoaded}
                      llmApiKey={llmApiKey} setLlmApiKey={setLlmApiKey}
                      llmUrl={llmUrl} setLlmUrl={setLlmUrl}
                      llmModel={llmModel} setLlmModel={setLlmModel}
                    />

                    <ChannelSection
                      active={devicePushedConfig || activeSection === "channel"}
                      channel={channel} setChannel={setChannel}
                      channelLoaded={channelLoaded}
                      teleToken={teleToken} setTeleToken={setTeleToken}
                      teleUserId={teleUserId} setTeleUserId={setTeleUserId}
                      slackBotToken={slackBotToken} setSlackBotToken={setSlackBotToken}
                      slackAppToken={slackAppToken} setSlackAppToken={setSlackAppToken}
                      slackUserId={slackUserId} setSlackUserId={setSlackUserId}
                      discordBotToken={discordBotToken} setDiscordBotToken={setDiscordBotToken}
                      discordGuildId={discordGuildId} setDiscordGuildId={setDiscordGuildId}
                      discordUserId={discordUserId} setDiscordUserId={setDiscordUserId}
                    />

                    <LanguageSection
                      active={devicePushedConfig || activeSection === "language"}
                      sttLanguage={sttLanguage} setSttLanguage={setSttLanguage}
                    />

                    <TTSSection
                      active={devicePushedConfig || activeSection === "tts"}
                      isContinue={isContinue}
                      ttsProvider={ttsProvider} setTtsProvider={setTtsProvider}
                      ttsProviders={ttsProviders}
                      ttsVoice={ttsVoice} setTtsVoice={setTtsVoice}
                      ttsVoices={ttsVoices}
                      sttLanguage={sttLanguage}
                    />
                  </div>

                  {isContinue && canEnrollVoice && (
                    <VoiceSection
                      active={activeSection === "voice"}
                      sttLanguage={sttLanguage}
                      faceOwners={faceOwners}
                      loadFaceOwners={loadFaceOwners}
                    />
                  )}

                  {isContinue && canEnrollFace && (
                    <FaceSection
                      active={activeSection === "face"}
                      faceOwners={faceOwners}
                      loadFaceOwners={loadFaceOwners}
                    />
                  )}

                  {stepError && (
                    <div className="lm-fade-in" style={{
                      fontSize: 12, color: C.red, marginBottom: 10,
                      display: "flex", alignItems: "center", gap: 6,
                    }}>
                      <span aria-hidden>⚠</span>{stepError}
                    </div>
                  )}

                  <div style={{
                    display: "flex", gap: 10, justifyContent: "space-between",
                    alignItems: "center", marginTop: 8,
                  }}>
                    {isFirstStep ? <span /> : (
                      <button
                        type="button"
                        onClick={goPrev}
                        className="lm-btn lm-btn-ghost"
                        style={{ padding: "9px 18px", fontWeight: 500 }}
                      >
                        ← Back
                      </button>
                    )}
                    {isLastStep ? (
                      isContinue ? (
                        <button
                          key="done"
                          type="button"
                          onClick={() => {
                            // Emit setup_done for either label: the parent closes the popup on it.
                            finishWizard();
                            setupBridge.monitorClicked();
                            navigate("/monitor");
                          }}
                          className="lm-btn lm-btn-primary"
                          style={{ padding: "9px 22px" }}
                        >
                          {isSkippableStep ? "Skip & finish →" : "Go to monitor →"}
                        </button>
                      ) : (
                        <button
                          // Distinct keys stop React mutating Next into Submit mid-click.
                          key="submit"
                          type="submit"
                          disabled={loading || loadingList}
                          className="lm-btn lm-btn-primary"
                          style={{ padding: "9px 22px" }}
                        >
                          {loading ? "Setting up…" : "Setup"}
                        </button>
                      )
                    ) : (
                      <button
                        key="next"
                        type="button"
                        onClick={goNext}
                        className="lm-btn lm-btn-primary"
                        style={{ padding: "9px 22px" }}
                      >
                        {isSkippableStep ? "Skip →" : "Next →"}
                      </button>
                    )}
                  </div>

                </form>
              </>
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
