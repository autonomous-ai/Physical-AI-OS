import { setupBridge } from "@/lib/setupBridge";
import type { SectionId } from "@/hooks/setup/types";
import { C } from "@/components/setup/shared";
import { WifiSection } from "@/components/setup/WifiSection";
import { LLMSection } from "@/components/setup/LLMSection";
import { ChannelSection } from "@/components/setup/ChannelSection";
import { LanguageSection } from "@/components/setup/LanguageSection";
import { TTSSection } from "@/components/setup/TTSSection";
// Voice/Face enrollment UI is shared with the Settings page (setting#voice /
// setting#face) so both entry points stay consistent — one implementation with
// the richer dropzone/camera/audio-player UX. Setup only supplies the shared
// faceOwners list + reload fn; these components own their own enroll state.
import { VoiceSection } from "@/pages/settings/VoiceSection";
import { FaceSection } from "@/pages/settings/FaceSection";
import { Wifi, Brain, Volume2, MessageSquare, UserCircle, Mic, Globe, Check } from "lucide-react";
import type { SetupMode } from "./helpers";
import { useSetupController } from "./useSetupController";
import { SetupProgressScreen } from "./SetupProgressScreen";
import { SetupSkeleton } from "./SetupSkeleton";

// Sidebar/tab icon per section id. Kept in the view layer (JSX) — the
// controller owns the icon-free SECTIONS list so its logic stays UI-agnostic.
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

// ── main page ─────────────────────────────────────────────────────────────────
// Thin view: all state/effects/handlers live in useSetupController; this file
// only renders. See useSetupController for the data-flow and the (unchanged)
// AP→STA / deep-link / bridge behavior.
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

  // The URL deep-links to a step that isn't visible yet (mode still resolving).
  // Hold the skeleton rather than painting Wi-Fi first: the requested tab is
  // usually one render away, and flashing the wrong step then jumping is what
  // this screen exists to prevent. Never blocks the post-submit progress screen —
  // that one owns the page once a join is in flight.
  if (awaitingDeepLink && !showProgressScreen) return <SetupSkeleton />;

  return (
    <div className={`lm-root lm-setup ${themeClass}`} style={{
      display: "flex", height: "100vh",
      background: C.bg, color: C.text,
      fontFamily: "'Inter', 'Segoe UI', sans-serif", fontSize: 14,
    }}>
      {/* ── Sidebar (hidden on mobile) ── */}
      <aside className="lm-sidebar" style={{
        width: 192, flexShrink: 0,
        background: C.sidebar, borderRight: `1px solid ${C.border}`,
        display: "flex", flexDirection: "column",
      }}>

        {/* Brand header + overall progress so the operator always sees how far
            they are into the wizard from the sidebar. */}
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
            // Show checks whenever a section's value is filled — including in
            // #force (initial) mode if the device already has saved config to
            // prefill from. A truly empty device still shows zero checks
            // because sectionDone returns false across the board.
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

      {/* ── Main ── */}
      <main style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}>

        {/* Mobile tabs (hidden on desktop). The theme toggle sits OUTSIDE the
            horizontally-scrolling tab row so it stays pinned (margin-left:auto
            doesn't pin inside an overflow-x flex container) and isn't hidden
            under the right-edge scroll-hint fade.
            Hidden entirely for a single-step flow (e.g. device-pushed config
            collapses the wizard to just Wi-Fi) — a lone tab chip is noise and
            looked like a stray label in the companion-app popup. */}
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

        {/* Topbar */}
        <div style={{ borderBottom: `1px solid ${C.border}`, flexShrink: 0 }}>
          <div style={{
            padding: "12px 24px 10px",
            display: "flex", alignItems: "center", justifyContent: "space-between",
          }}>
            <span style={{ fontSize: 15, fontWeight: 600, color: C.text }}>
              {showProgressScreen ? "Setting up…" : visibleSections.find((s) => s.id === activeSection)?.label ?? "Wi-Fi"}
            </span>
            {/* "Step X / Y" only makes sense for a multi-step wizard. With a
                single visible step (V2's merged flow) it's noise, so hide it. */}
            {!showProgressScreen && visibleSections.length > 1 && (
              <span style={{ fontSize: 12, color: C.textDim }}>
                Step {currentStepIndex + 1} / {visibleSections.length}
              </span>
            )}
          </div>
          {/* Per-step progress mirrors the wizard position (not section-done
              count) so the bar advances as the operator walks Back/Next.
              Hidden for a single-step flow — there's no progress to show when
              Wi-Fi is the only step (device-pushed config). */}
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

        {/* Content */}
        <div ref={contentRef} className="lm-fade-in lm-main-content" style={{
          flex: 1, minHeight: 0, overflowY: "auto", padding: "24px 32px",
        }}>
          <div style={{ maxWidth: 560, margin: "0 auto" }}>

            {/* Post-submit screen: shows progress while the device joins
                Wi-Fi, then a QR + IP for the user to continue setup on the
                home network once the AP shuts down. */}
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

                  {/* Device ID + MAC live purely in controller state now — no
                      DOM section renders for them. Submit reads deviceId
                      directly from state; the hidden admin password flows
                      through WifiSection below, defaulted server-side when
                      empty (see handler.Setup). */}

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

                  {/* When devicePushedConfig is on, the four sections below are
                      kept mounted but visually hidden — their state autofills
                      from URL params and still flows through the form submit. */}
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

                  {/* Mounted only when the device declares the hardware each
                      one drives — a mic for voice enrollment, a camera for
                      face. Matches the sidebar gate in the controller, and
                      keeps a section that can't work from issuing its
                      hardware requests on mount. */}
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
                        // Continue mode = device already provisioned + on
                        // home Wi-Fi. Voice / Face are optional enrollments,
                        // so the last step shouldn't re-trigger setup — send
                        // the user to /monitor instead. Re-submit only
                        // happens in initial mode (last step = wifi or tts).
                        <button
                          key="done"
                          type="button"
                          onClick={() => {
                            // Both labels end the wizard — "Skip & finish"
                            // because the operator declined the last optional
                            // step, "Go to monitor" because they completed it.
                            // Emit setup_done for either: the parent closes the
                            // popup on that event, and gating it on the skip
                            // variant meant finishing the wizard properly left
                            // the popup open. monitor_clicked still follows as
                            // the navigation detail.
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
                          // Distinct keys prevent React from mutating a single
                          // <button> element from type="button" (Next) to
                          // type="submit" (Setup) in place. Without separate
                          // keys the in-flight click on Next can land on the
                          // mutated Submit button and trigger an unwanted form
                          // submission.
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
