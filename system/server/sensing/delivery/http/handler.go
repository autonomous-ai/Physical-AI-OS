package http

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/go-playground/validator/v10"

	"go.autonomous.ai/os/system/agentfile"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/lib/safego"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/lib/usercanon"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/skillcontext/mood"
	"go.autonomous.ai/os/system/skillcontext/musicsuggestion"
	"go.autonomous.ai/os/system/skillcontext/posture"
	"go.autonomous.ai/os/system/skillcontext/wellbeing"
	"go.autonomous.ai/os/system/statusled"
	"go.autonomous.ai/os/system/telemetry"
	"go.autonomous.ai/os/system/vision"
)

// Explicit Harness/agent wording can select the skill; bare names only warrant discovery.
// realtimeDelegationPrefix opens the message HAL sends when the realtime
// model hands a turn to the main agent (hal/drivers/voice/_internal/turn_dispatch.py).
const realtimeDelegationPrefix = "[voice-instruction]"

var harnessAgentRequest = regexp.MustCompile(`(?i)(?:^|[^\p{L}\p{N}_])(?:ask|tell|have|message|use|delegate(?:\s+to)?|check(?:ing)?\s+with|hỏi|bảo|nhờ|kêu|hoi|bao|nho|keu|dùng|dung)\s+(?:(?:the|a|an|một|mot)\s+)?(?:(?:harness|agent)(?:\s|$)|[\p{L}\p{N}_-]+\s+agent(?:\s|$))`)
var harnessPossibleNamedRequest = regexp.MustCompile(`(?i)(?:^|[^\p{L}\p{N}_])(?:ask|tell|message|check(?:ing)?\s+with|hỏi|bảo|nhờ|kêu|hoi|bao|nho|keu)\s+([\p{L}\p{N}_-]+)(?:\s|$)`)
var buddyAgentRequest = regexp.MustCompile(`(?i)\b(?:autonomous\s+buddy|(?:ask|tell|use|with|via|nhờ|hỏi|bảo|nho|hoi|bao)\s+(?:the\s+)?buddy)\b`)

const harnessNamedAgentRouting = "[system-routing: For explicit Harness delegation, use harness-use to select an agent for the task. A named Harness agent is the execution target, not a person to contact. Use harness-use only. Do not call agent-management, computer-use, Autonomous Buddy, or /api/buddy. An explicitly requested agent takes priority over any retained target: list real agents and select the exact requested agent, including when the user changes agents. For a new task without an explicit name, follow the skill task-based selection policy (including Store discovery/preparation when a suitable existing agent is unavailable; never replace an unavailable explicit target): list agents and use each agent's recap headline as the first evidence of its current project and work; when the headlines do not settle it, read only the newest recap/text pair (recap n:1, turns[0]) of at most two candidates and match the task against that text; choose the agent whose recap matches the task; a missing recap is unknown, not availability; do not assume the retained agent is suitable. Retain a target only for a clear continuation of its task; when several agents could own the referenced task, continue with the one whose recap describes it, otherwise ask. Then send the selected agent the underlying task directly with an explicit agentId and the harness-reply routing object. Use the stable conversation scope, never the per-turn run_id as conversation_id. When correcting a wrong target, recover the original unfinished user request and transfer it to the right task/workspace; do not turn a complaint about the wrong workspace into a different task or invent replacement work. Remove the leading delegation wording from the task: for example, \"Ask David if there are events in the US\" must be sent to David as \"Find upcoming events in the US\", never as a request to ask or contact David. If a prior delivery blocks this new or corrected task, inspect its receipt once. If it is delivered, started, completed, or rejected, immediately send the user's current task in this same turn; never return NO_REPLY until that new send/answer has a known receipt. If send/answer returns queued, delivered, started, completed, or rejected, it has a known outcome: make no more Harness or shell calls (including receipt, status, recap, list, or another send), and immediately reply NO_REPLY. Inspect receipt only for DeliveryUnknown/no usable receipt or an explicit user delivery-state request; never resend automatically.]"
const harnessFollowupRouting = "[system-routing: A Harness task or question may be awaiting a follow-up. An explicit new target, a new Harness task (including digital work delegated by the Lamp persona without naming Harness), or a request for Buddy takes priority over this follow-up hint. For a new task, read harness-use and select a suitable agent or use its negotiated Store preparation flow under the device persona policy instead of inheriting the retained target. A continuation of unfinished Store preparation resumes its saved intent via workflow-status/operation, not a new task send. Use the retained Harness target only when the user's words clearly continue the retained Harness task, answer its currently open question, or ask whether it has finished or for its result. Do not treat vague fragments, acknowledgements, filler, unrelated new requests, or uncertain speech as continuations; let the main agent handle those under its normal persona and skill policy. For a continuation, read harness-use context when the task owner is not explicit in the current conversation, resolve the correct task agent and pass its explicit agentId to send/answer. Never omit agentId to inherit a default target, and never use the per-turn run_id as conversation_id. A correction of the execution target keeps the unfinished original task: select the right task/workspace and send the original requested change there, without attributing another workspace's mistake to it. If an agent reports a missing scene/file, recheck task ownership; do not authorize creating a replacement scene or add fallback work the user did not request. Only when the user's words could continue a task of a different Harness agent, compare them with the listed agents' recap headlines: if exactly one listed agent's recap describes that task, continue with that agent instead; if several do, ask which task; a missing recap is unknown, not a reason to switch. Do not use Buddy and do not answer a clear Harness follow-up yourself. For a clear continuation with the skill already loaded, do not reread the skill/directory or run harness.py --help. If send/answer returns queued, delivered, started, completed, or rejected, make no more Harness or shell calls (including receipt, status, recap, list, or another send) and immediately reply NO_REPLY. Inspect receipt only for DeliveryUnknown/no usable receipt or an explicit user delivery-state request; never resend automatically.]"

const harnessAgentDiscoveryRouting = "[system-routing: The user may be naming an agent or a person. This wording alone does not authorize Harness delegation. If context indicates a Harness agent request, use harness-use to list real agents and resolve the requested name before selecting or sending; an explicit new name overrides the retained target. Otherwise handle the request normally. Do not treat ordinary contact requests as agent tasks.]"

// harnessRequestRouting keeps a stale follow-up target from overriding a new request.
func harnessRequestRouting(message string, followupActive bool) string {
	if buddyAgentRequest.MatchString(message) {
		return ""
	}
	if harnessAgentRequest.MatchString(message) {
		return harnessNamedAgentRouting
	}
	if match := harnessPossibleNamedRequest.FindStringSubmatch(message); len(match) > 1 {
		// Pronouns and common task words are not evidence of a named agent.
		switch strings.ToLower(match[1]) {
		case "me", "my", "us", "them", "him", "her", "someone", "the", "a", "an", "if", "whether", "about", "how", "what", "why", "when", "where", "you", "your", "tôi", "tui", "toi", "mình", "minh", "bạn", "ban", "thời", "thoi", "kiểm", "kiem":
		default:
			return harnessAgentDiscoveryRouting
		}
	}
	if followupActive {
		return harnessFollowupRouting
	}
	return ""
}

const maxHarnessFollowupContextRunes = 6000

func truncateHarnessFollowupContext(text string) string {
	runes := []rune(strings.TrimSpace(text))
	if len(runes) <= maxHarnessFollowupContextRunes {
		return string(runes)
	}
	return string(runes[:maxHarnessFollowupContextRunes]) + "…"
}

// SensingEventRequest is the payload from HAL sensing detectors.
type SensingEventRequest struct {
	// VoiceTurnType records wake admission for diagnostics, never routing.
	VoiceTurnType string `json:"voice_turn_type,omitempty"`
	// Type is the event category: motion, sound, presence.enter, presence.leave, light.level, etc.
	Type string `json:"type" validate:"required"`
	// Message is a natural-language description of what was detected.
	Message string `json:"message" validate:"required"`
	// Images are optional base64-encoded JPEG snapshots. A camera event attaches
	// exactly one (large motion, face detected) so the AI can see; a chat client
	// can attach several at once. A slice, not a single string: every wire format
	// downstream already carries `attachments[]`, so nothing here has to choose
	// which photo survives.
	Images []string `json:"images,omitempty"`
	// InteractionID correlates task metrics. HAL supplies it for voice; OS
	// generates one for chat or selected sensing when absent. For voice it is
	// echoed back as the owner of audio os-server starts for this turn. The opening
	// filler fires before this request's response reaches HAL, so HAL's own
	// run-id binding cannot cover it.
	InteractionID string `json:"interaction_id,omitempty"`
	// CurrentUser is HAL's view of who is effectively in front of the device
	// right now (from FaceRecognizer.current_user()). Empty when nobody is
	// visible. This is the source of truth — do NOT re-derive by parsing
	// Message. Text parsing gave wrong answers when a stranger-only enter
	// event fired while a friend was still present (the agent would downgrade
	// mood to "unknown" even though the friend was within forget window).
	CurrentUser string `json:"current_user,omitempty"`
	// Audio is an optional path (on the Pi) to the WAV clip that produced this
	// event — currently only speech_emotion.detected, carrying the latest clip
	// of the dominant label this flush. It is a DEBUG aid surfaced in the Flow
	// Monitor UI as a clickable player; it is NEVER forwarded to the LLM (the
	// field is not part of Message and is never concatenated into the outgoing
	// chat text). Served back to the UI via GetAudio.
	Audio string `json:"audio,omitempty"`
	// File is an optional NON-IMAGE attachment from a chat client (a PDF, a
	// CSV). Kept separate from Image because the two need opposite handling: an
	// image goes through the describe-first vision gate, a document must not —
	// it would fail there, and before this field existed every attachment rode
	// the Image field and was written as `.jpg` regardless of what it was.
	Files []domain.InboundFile `json:"files,omitempty"`
	// HarnessVoice is the routing snapshot taken by HAL before voice capture.
	// It is deliberately separate from Message and is never forwarded to a model.
	HarnessVoice *HarnessVoiceSnapshot `json:"harness_voice,omitempty"`
}

type HarnessVoiceSnapshot struct {
	Enabled    bool   `json:"enabled"`
	Generation uint64 `json:"generation"`
}

// SensingHandler handles incoming sensing events from HAL and forwards them to the agent.
type SensingHandler struct {
	intentResolver   *jev.Resolver
	agentGateway     domain.AgentGateway
	monitorBus       *monitor.Bus
	config           *config.Config
	statusLED        *statusled.Service
	voiceActiveUntil atomic.Int64 // unix ms; set on voice_listening, extended on voice_listening_end
	isSleeping       func() bool  // true when the device is asleep; HAL decides, see AgentHandler.IsSleeping
	lastNotReadyTTS  atomic.Int64 // unix ms; cooldown for "brain restarting" TTS
	lastAgentTurn    atomic.Int64 // unix ms of the last agent turn created here — ambient floor reference

	// onRealtimeHandled, when set, is called once per voice_agent_handled
	// event: the realtime agent has spoken an answer to a newer utterance, so
	// the agent handler mutes the older turn still in flight. A callback rather
	// than a direct dependency, following isSleeping above — this package must
	// not import the agent delivery package it is a sibling of.
	// Returns whether os-server ACTUALLY suppressed the older turn's speech.
	// HAL needs the answer, not an assumption: automatic supersession is
	// opt-in (OS_REALTIME_SUPERSEDES_MAIN_REPLY), so on a default body nothing
	// is suppressed and the situation is not a metric sample at all.
	onRealtimeHandled      func() bool
	realtimeHistory        func(string, string) (string, error)
	harnessConnected       func() bool
	harnessFollowup        func() bool
	harnessTaskPending     func() bool
	harnessFollowupContext func() string
	harnessVoice           func(*gin.Context, SensingEventRequest) bool
}

// SetHarnessVoice installs the direct voice route before local intents or runtime gates.
func (h *SensingHandler) SetHarnessVoice(fn func(*gin.Context, SensingEventRequest) bool) {
	h.harnessVoice = fn
}

// SetOnRealtimeHandled installs the realtime-handled hook. Wired in
// ProvideServer, where both handlers exist; left nil in tests and by any
// caller that does not route voice through the realtime agent.
func (h *SensingHandler) SetOnRealtimeHandled(fn func() bool) {
	h.onRealtimeHandled = fn
}

// SetHarnessConnected supplies the current paired transport state.
func (h *SensingHandler) SetHarnessConnected(fn func() bool) { h.harnessConnected = fn }

func (h *SensingHandler) SetHarnessFollowup(fn func() bool) { h.harnessFollowup = fn }

// SetHarnessFollowupContext supplies the latest direct Harness result for a
// short user clarification. The source is untrusted remote-agent output.
func (h *SensingHandler) SetHarnessFollowupContext(fn func() string) {
	h.harnessFollowupContext = fn
}

// ProvideSensingHandler constructs a SensingHandler.
func ProvideSensingHandler(gw domain.AgentGateway, bus *monitor.Bus, cfg *config.Config, sled *statusled.Service, isSleeping func() bool) *SensingHandler {
	// Gate local intent rules to what this device's body can do — set once here.
	intent.Configure(device.Capabilities(cfg.DeviceTypeOrDefault()))
	sensingmsg.SetEnvironmentReplayAllowed(func() bool {
		return cfg.EnvironmentSettings().Enabled && device.Capabilities(cfg.DeviceTypeOrDefault())[device.CapEnvironment] &&
			(isSleeping == nil || !isSleeping())
	})
	// Social talk belongs to whoever answers first. With the realtime agent on,
	// it takes every voice turn before os-server sees one and replies in
	// character — so local chitchat would only ever fire on turns it stayed
	// silent for, barging in with a canned line in another voice. Command
	// intents (lights, volume, time) stay on regardless: those genuinely beat
	// the model. Re-evaluated on every config change (see runConfigChangeListener).
	intent.SetChitchatEnabled(!cfg.RealtimeEnabled())
	return &SensingHandler{
		intentResolver: jev.NewResolver(),
		agentGateway:   gw,
		monitorBus:     bus,
		config:         cfg,
		statusLED:      sled,
		isSleeping:     isSleeping,
	}
}

// PostEvent receives a sensing event and sends it to the agent as a chat message.
// Voice and text-only chat events first try local intent rules and Jev.
func (h *SensingHandler) PostEvent(c *gin.Context) {
	var req SensingEventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	// Environment is optional: unknown or unreadable declarations must not
	// enable passive environment turns on devices without this capability.
	if req.Type == "environment.update" && !device.Capabilities(h.config.DeviceTypeOrDefault())[device.CapEnvironment] {
		c.JSON(http.StatusForbidden, serializers.ResponseError("environment capability not declared"))
		return
	}
	if req.Type == "environment.update" && !h.config.EnvironmentSettings().Enabled {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped_disabled"}))
		return
	}

	slog.Info("sensing event received", "component", "sensing", "type", req.Type, "message", req.Message)

	// Wake-word command or authorized follow-up from physical device — log for
	// tracing (LED feedback is handled by HAL).
	if req.Type == "voice_command" || req.Type == "voice_followup" {
		slog.Info("authorized voice received", "component", "sensing", "type", req.Type, "message", req.Message)
	}
	// voice_listening / voice_listening_end are internal LED signals — don't forward to agent.
	// Also gate sensing events: suppress passive sensing during the voice conversation window
	// so motion/presence can't steal the agent turn while the user is speaking or waiting for reply.
	if req.Type == "voice_listening" {
		// Extend window: user is speaking, keep sensing suppressed for 10s from now.
		h.voiceActiveUntil.Store(time.Now().Add(10 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}
	if req.Type == "voice_listening_end" {
		// Extend window 5s to cover STT → os-server → LLM → TTS pipeline.
		h.voiceActiveUntil.Store(time.Now().Add(5 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}

	// User tasks enter the cohort at receipt, including queued chat. Sensor
	// notifications enter only once routing selects an actual dispatch below.
	// Only HAL-supplied interaction IDs can own follow-up focus. A telemetry
	// ID generated locally below has no authorized capture to bind in HAL.
	followupInteractionID := req.InteractionID
	taskGroup := telemetry.TaskGroup(req.Type)
	if taskGroup == "voice" || taskGroup == "chat" {
		req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, "")
	}
	startPayload := map[string]any{"type": req.Type, "message": req.Message, "interaction_id": req.InteractionID}
	if kind := req.voiceTurnType(); kind != "" {
		startPayload["voice_turn_type"] = kind
	}

	// look.capture is MONITOR-ONLY. The realtime `look` tool already sent the
	// frame straight to the model, so forwarding text here would inject a
	// phantom turn the user never asked for. Log the flow event — the monitor
	// derives the thumbnail from the frame path in the message — then stop.
	// Unlike motion.activity (snapshot shown but stripped before the LLM), this
	// frame IS the model's input, which is what makes it worth surfacing.
	if req.Type == "look.capture" {
		lookRunID := fmt.Sprintf("look-%d", time.Now().UnixMilli())
		lookStart := flow.Start("sensing_input", startPayload, lookRunID)
		flow.End("sensing_input", lookStart, map[string]any{"type": req.Type}, lookRunID)
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}

	// Push sensing input to monitor.
	monitorDetail := map[string]any{"type": req.Type}
	if kind := req.voiceTurnType(); kind != "" {
		monitorDetail["voice_turn_type"] = kind
	}
	// Surface the debug audio clip (speech_emotion) to the Flow Monitor UI only
	// — as a servable URL, never the raw path, and never to the LLM.
	if audioURL := audioURLForPath(req.Audio); audioURL != "" {
		monitorDetail["audio"] = audioURL
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_input",
		Summary: "[" + req.Type + "] " + req.Message,
		Detail:  monitorDetail,
	})

	// Sync mood.CurrentUser with HAL's view on every event that carries
	// it. HAL's FaceRecognizer.current_user() is the source of truth.
	//
	// Wellbeing enter/leave rows are written by HAL directly (per
	// friend on their own timeline, stranger collapsed to "unknown"
	// timeline) — the handler no longer writes them here. See
	// faceid/perception.py _post_wellbeing.
	// speech_emotion.detected is exempt: SER identifies nobody. Its
	// current_user is a courier value computed by the voice turn, and five
	// unrelated situations collapse into the literal string "unknown" —
	// speaker-ID found no enrolled match, speaker-ID could not run, the turn
	// had no transcript at all, the wake-word gate rejected it, or the noise
	// guard dropped it. Letting that write here means one ambient sigh from an
	// unrecognized voice erases a live face-derived identity.
	//
	// Nothing is lost by skipping it. SER inherits its user from speaker-ID
	// only (never face), and a confident speaker-ID match is already promoted
	// device-wide by voice_service.py set_voice_user() before the SER event is
	// even queued — so every other producer (voice turns, sensing) is already
	// shipping that identity via app_state.resolve_current_user(), where face
	// outranks voice. SER's copy is at best a duplicate, and always the
	// latest-arriving one (queue + cloud call + flush window).
	//
	// This does NOT change the event's own attribution: the message the agent
	// sees still carries "[context: current_user=...]" built from
	// req.CurrentUser below, so stranger mood still logs under "unknown" as
	// skills/mood/SKILL.md requires.
	if req.CurrentUser != "" && req.Type != "speech_emotion.detected" {
		mood.SetCurrentUser(req.CurrentUser)
	} else if req.Type == "presence.leave" || req.Type == "presence.away" {
		mood.ClearCurrentUser()
	}

	if h.harnessVoice != nil && h.harnessVoice(c, req) {
		return
	}

	// Keep attachments on the agent path so intent matching cannot discard them.
	isVoice := req.Type == "voice" || req.Type == "voice_command" || req.Type == "voice_followup"
	isChat := sensingmsg.IsChat(req.Type)
	if (isVoice || isChat) && len(req.Images) == 0 && len(req.Files) == 0 && h.config.LocalIntentEnabled() && !h.deferContextualIntent(req.Message) {
		if result := h.matchVoiceIntent(c.Request.Context(), req.Message); result != nil {
			// Generate a dedicated local-intent trace ID so this turn doesn't
			// share the global trace of an in-flight agent turn.
			localRunID := fmt.Sprintf("local-intent-%d", time.Now().UnixNano())
			telemetry.ReportTaskStarted(req.Type, req.InteractionID, localRunID)
			turnStart := flow.Start("sensing_input", startPayload, localRunID)
			source := "local"
			if result.Source != "" {
				source = result.Source
			}
			flow.Log("intent_match", map[string]any{"message": req.Message, "tts": result.TTSText, "rule": result.Rule, "actions": result.Actions, "source": source}, localRunID)
			if result.TTSText != "" && isVoice {
				owner := req.InteractionID
				go func() {
					// Cached path: fixed phrases like "Volume up!" hit the
					// WAV cache (~50ms) instead of going through ElevenLabs
					// (~1.5s). Dynamic texts (time, color) miss + render once.
					//
					// owner: a locally-handled command is answered here and
					// never gets a run id, so without it the reply the user
					// actually hears would be unattributed audio.
					if err := hal.SpeakCachedForTurn(result.TTSText, owner); err != nil {
						slog.Warn("intent TTS failed", "component", "sensing", "error", err)
					}
				}()
			}
			// Signal ambient service about LED state changes
			if result.LEDChanged {
				h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "intent: " + req.Message})
			} else if result.LEDOff {
				h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "intent: " + req.Message})
			}
			if result.Emotion != "" {
				h.monitorBus.Push(domain.MonitorEvent{Type: "emotion", Summary: result.Emotion})
			}
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "intent_match",
				Summary: "[" + source + "] " + req.Message + " → " + result.TTSText,
			})
			flow.End("sensing_input", turnStart, map[string]any{"path": "local"}, localRunID)
			if result.ExecutionFailed {
				telemetry.ReportTaskExecution(localRunID, req.InteractionID, "failed", "local_intent_error")
			} else {
				telemetry.ReportTaskExecution(localRunID, req.InteractionID, "completed", "local_intent_returned")
			}
			slog.Info("intent handled", "component", "sensing", "type", req.Type, "source", source, "intent", result.Rule, "run_id", localRunID)
			flow.Log("agent_response", map[string]any{"text": result.TTSText, "source": source}, localRunID)
			c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
				"localRunId": localRunID,
				"handler":    "local",
				"response":   result.TTSText,
				// Keep runId absent: web chat renders the immediate response,
				// while MQTT correlates its final event using localRunId.
				"handledLocally": "true",
			}))
			return
		}
	}

	// Sleep guard: while the agent is in "sleepy" state, drop all passive sensing
	// (light.level, motion, sound) so they don't wake the agent and override the
	// sleepy emotion. Only presence.enter, fire_hazard.detected, authorized
	// voice commands/follow-ups, and realtime-handled turns pass through.
	// Typed chat (web_chat from the monitor composer, mqtt_chat from chat.send)
	// is user-initiated text — bypasses sleep-drop (forwarded to agent, TTS
	// suppressed) but does NOT trigger physical wake. It counts as passive for
	// the busy-gate so it queues on agent busy instead of racing the in-flight
	// turn (agent merges same-session messages).
	isVoiceCommand := req.Type == "voice_command" || req.Type == "voice_followup"
	// A realtime-handled turn is user-initiated by definition: the user spoke and
	// the realtime agent ALREADY replied out loud. That exchange happens entirely
	// in HAL and never consults this sleep flag, so the device can be "asleep"
	// here while it is actively holding a conversation. Dropping the event costs
	// the main agent its [HANDLED]/[REPLY] memory sync — the conversation happened
	// and the agent has no record of it. Kept OUT of isVoice deliberately: that
	// flag also fires the opening filler below, which must never play for a turn
	// the realtime agent already answered (the run is MarkSilentRun).
	isRealtimeHandled := req.Type == "voice_agent_handled"
	// The realtime agent has just answered a NEWER question out loud, so the
	// main-agent turn still working on the previous one loses the speaker (see
	// CancelSpeechForNewerTurn).
	//
	// This sits BEFORE the busy fork on purpose. voice_agent_handled counts as
	// passive, so when the agent is busy it is queued and returns early — and
	// "the agent is busy" is exactly the case with an older turn still in
	// flight. Hooking it further down, next to MarkSilentRun, makes the whole
	// thing a no-op precisely when it is needed. The mark is about wall-clock
	// "the user has already been answered", which holds whether or not the
	// sync event itself reaches the agent now.
	speechSuppressed := false
	if isRealtimeHandled && h.onRealtimeHandled != nil {
		speechSuppressed = h.onRealtimeHandled()
	}
	if isRealtimeHandled && h.realtimeHistory != nil {
		h.persistRealtimeHistory(c, req, speechSuppressed)
		return
	}
	isPassive := !isVoiceCommand
	if isPassive && !isVoice && !isRealtimeHandled && !isChat && req.Type != "presence.enter" && req.Type != "fire_hazard.detected" && h.isSleeping != nil && h.isSleeping() {
		slog.Info("INBOUND from HAL → SLEEP-DROPPED (lamp sleeping)",
			"component", "sensing", "backend", h.agentGateway.Name(), "type", req.Type)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "sensing_drop",
			Summary: "[" + req.Type + "] " + req.Message,
			Detail:  map[string]any{"type": req.Type, "reason": "sleeping"},
		})
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped_sleeping"}))
		return
	}

	// Global cross-type floor for ambient sensing turns: at most one agent
	// turn per SensingTurnFloorSeconds across ALL ambient event types.
	// Per-event cooldowns live in HAL, but they are independent per type —
	// without this floor a burst of different types (emotion + sound +
	// motion within seconds, typical right after a presence change) still
	// costs several agent turns. The clock is updated by EVERY turn this
	// handler creates (voice, web_chat, presence, fire included), so
	// ambient events stay quiet for the floor window after any interaction.
	// Trade-off: a floored drop can make a HAL-side dedup believe "sent" —
	// acceptable, every ambient emitter re-offers on its own heartbeat.
	// Guard mode bypasses the floor for surveillance events; environment
	// updates remain advisory and retain the floor. Placed BEFORE the describe
	// gate so a floored event never spends a vision-describe API call.
	if floorS := h.config.SensingTurnFloorSeconds(); floorS > 0 &&
		ambientFloorTypes[req.Type] && (!h.config.GuardModeEnabled() || req.Type == "environment.update") {
		if sinceMs := time.Now().UnixMilli() - h.lastAgentTurn.Load(); sinceMs < int64(floorS)*1000 {
			slog.Info("INBOUND from HAL → FLOOR-DROPPED (ambient turn floor)",
				"component", "sensing", "backend", h.agentGateway.Name(),
				"type", req.Type, "sinceS", sinceMs/1000, "floorS", floorS)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "sensing_drop",
				Summary: "[" + req.Type + "] " + req.Message,
				Detail:  map[string]any{"type": req.Type, "reason": "ambient_floor", "since_s": sinceMs / 1000, "floor_s": floorS},
			})
			c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped_floor"}))
			return
		}
	}

	// Voice wake: when a voice command arrives while sleeping, fire greeting emotion
	// to HAL so it wakes up (LED + servo) before the agent processes the turn.
	// Without this, the agent's emotion:thinking would be blocked by HAL's wake guard.
	// web_chat skips wake — typing in the monitor isn't a request for physical interaction.
	if isVoiceCommand && h.isSleeping != nil && h.isSleeping() && device.Has(h.config.DeviceTypeOrDefault(), device.CapExpression) {
		slog.Info("voice wake — firing greeting to wake HAL", "component", "sensing")
		go func() {
			if err := hal.SetEmotion("greeting", 0.8); err != nil {
				slog.Warn("voice wake greeting failed", "component", "sensing", "error", err)
				return
			}
			slog.Info("voice wake greeting sent", "component", "sensing")
		}()
	}

	// Chat with image (web composer or MQTT chat.send): save to temp file so
	// agent can reference the path (e.g. for face enrollment — tools read the
	// file directly, no LLM vision needed). Tag uses [image:] not [snapshot:]
	// to avoid the strip below.
	// Done here, BEFORE the busy fork, so a queued turn carries the tag too.
	if isChat {
		for i, img := range req.Images {
			if img == "" {
				continue
			}
			imgData, derr := base64.StdEncoding.DecodeString(img)
			if derr != nil {
				continue
			}
			// Index in the name so several photos attached to ONE turn cannot
			// collide on the same millisecond and overwrite each other.
			tmpPath := fmt.Sprintf("/tmp/web-chat-%d-%d.jpg", time.Now().UnixMilli(), i)
			if werr := os.WriteFile(tmpPath, imgData, 0644); werr == nil {
				req.Message += "\n[image: " + tmpPath + "]"
			}
		}
	}

	// Non-image attachment: land it with its REAL extension and tag it as a
	// file, not an image. Deliberately its own branch rather than more work in
	// the block above — a document must skip the describe-first gate below,
	// which keys off req.Images. Same placement, BEFORE the busy fork, so a
	// queued turn replays carrying the tag.
	//
	// This is the one place both chat paths converge: the web composer POSTs
	// here directly, and the MQTT chat.send handler re-enters over loopback, so
	// a phone and a browser attach files through identical code.
	for i, f := range req.Files {
		if f.Content == "" {
			continue
		}
		// Index folded into the millisecond stamp so several files attached to
		// ONE turn cannot collide on the same generated name.
		path, ferr := agentfile.SaveInbound("/tmp", f.Name, f.Content, time.Now().UnixMilli()+int64(i))
		if ferr != nil {
			// Best-effort per file: the turn still runs with whatever saved. The
			// user's question usually stands on its own.
			slog.Warn("chat attachment not saved", "component", "sensing",
				"name", f.Name, "error", ferr)
			continue
		}
		name := strings.TrimSpace(f.Name)
		if name == "" {
			name = filepath.Base(path)
		}
		// Both the display name and the path: the agent needs the path to
		// open the file, and the name is what the user will call it.
		req.Message += fmt.Sprintf("\n[file: %s (%s)]", path, name)
	}

	// Describe-first gate — must also run BEFORE the busy fork: a queued event
	// replays through the runtime drain paths (openclaw service_events.go and
	// friends), which send raw attachments with no gate of their own. A raw
	// attachment 404s at the smart-agent-router when the text-only main model
	// (Auto-AI) is active ("No endpoints found that support image input"), so
	// convert image→text here once and every downstream path — queued or
	// direct, any runtime — forwards text the model can use. Vision-capable
	// main models (per the catalog) skip this and get the raw attachment.
	// On describe failure (after retry) the image is DROPPED, not attached:
	// a raw attachment sometimes works (router lands on a vision model) but
	// when it doesn't, the image block sticks in the session history and 404s
	// every later turn routed to a text-only model — one bad turn is cheaper
	// than a poisoned conversation. Slash commands keep the raw attachment;
	// motion.activity images never reach the agent at all.
	if len(req.Images) > 0 && req.Type != "motion.activity" &&
		!(isChat && strings.HasPrefix(strings.TrimSpace(req.Message), "/")) &&
		!vision.ModelSupportsVision(h.config) {
		// One describe call per attached photo — a chat client can attach
		// several, and a text-only main model can only ever see the text these
		// produce. Numbered when there is more than one so the agent can tell
		// the user which photo it is talking about. A failure is per-image: the
		// ones that did describe still reach the model.
		//
		// CONCURRENT, not sequential: this runs inside the HTTP handler, so the
		// caller's POST does not return until every describe finishes. A single
		// describe measured 8-38 s, so two photos in series left the web chat
		// with a silent composer for ~53 s (lamp-0c89, 2026-09-03) — long enough
		// that reloading the page is the natural move, which cancels the request
		// and loses the turn. Fanning out makes the wait the SLOWEST image
		// instead of their sum. Results are written by index so the numbering
		// still matches the order the user attached them in.
		results := make([]string, len(req.Images))
		errs := make([]error, len(req.Images))
		var wg sync.WaitGroup
		for i, img := range req.Images {
			if img == "" {
				continue
			}
			wg.Add(1)
			// safego, not a bare goroutine: a panic inside describe (a malformed
			// response body has done it) must not take the whole os-server down
			// on a user-attached photo. wg.Done runs via the wrapper's defer.
			safego.Go("sensing-describe", func() {
				defer wg.Done()
				d, e := vision.DescribeWithRetry(h.config, img, req.Message)
				if e != nil {
					errs[i] = e
					return
				}
				if len(req.Images) > 1 {
					d = fmt.Sprintf("(image %d of %d) %s", i+1, len(req.Images), d)
				}
				results[i] = d
			})
		}
		wg.Wait()
		descs := make([]string, 0, len(results))
		var derr error
		for i, d := range results {
			if d != "" {
				descs = append(descs, d)
			} else if errs[i] != nil {
				derr = errs[i]
			}
		}
		desc := strings.Join(descs, "\n")
		// Only a describe that produced NOTHING is treated as a failure: a
		// partial result is still worth more to the answer than the "couldn't
		// see it" notice below.
		if len(descs) > 0 {
			derr = nil
		}
		// Either way the snapshot file must go: it sits inside the agent's
		// media allow-list, so any path the agent digs up later (old hints in
		// session history, an exec `ls` of the dir) could still be `read`
		// into an image block. Described → nothing needs it; describe failed
		// → it must never reach the LLM. Best-effort; the hint rewrite is
		// the primary guard.
		removeVisionSnapshot(req.Message)
		if derr != nil {
			slog.Warn("vision describe failed after retry — dropping image (text-only main model)",
				"component", "sensing", "type", req.Type, "error", derr)
			if reVisionImageHint.MatchString(req.Message) {
				req.Message = reVisionImageHint.ReplaceAllString(req.Message,
					"[vision-image] (a photo was captured but could not be processed — tell the user you couldn't see it this time; do NOT guess what was in it, do NOT take a new snapshot, do NOT read any image file)")
			} else {
				req.Message += "\n[image unavailable] the attached photo could not be processed — tell the user you couldn't see it this time; do NOT guess what was in it"
			}
		} else {
			// Drop the snapshot path from the [vision-image] hint — with a
			// description below, the agent must not read the image file (an
			// image tool result poisons the session history for text-only
			// routed models; see reVisionImageHint).
			req.Message = reVisionImageHint.ReplaceAllString(req.Message,
				"[vision-image] (a photo was just captured for this request; answer the visual question from the [image description] below — do NOT take a new snapshot, do NOT read any image file)")
			req.Message += "\n[image description] " + desc
		}
		req.Images = nil // text-only from here on; nothing downstream gets the blobs
	}

	// When agent is busy:
	// - voice_command / voice_followup (wake-word authorized) always pass through immediately.
	// - voice (ambient STT), presence.enter/leave are queued and replayed when agent becomes idle.
	// - During voice window: all passive sensing is queued (not dropped) so events aren't lost.
	// - Outside voice window: motion/light/sound dropped when busy (low priority, high frequency).
	inVoiceWindow := time.Now().UnixMilli() < h.voiceActiveUntil.Load()
	// "The agent is free" is not the same as "the device is free". A runtime
	// goes idle the moment its reply text is handed to the TTS queue, while
	// that reply keeps playing for tens of seconds — and HAL gives the speaker
	// to the newest turn, so an event forwarded during that window cuts the
	// answer the user actually asked for. The queue-and-replay path already
	// waits for the speaker (lib/speakergate); this is the same rule for an
	// event that arrives with no turn in flight at all. Probed only when the
	// agent is idle and the type is one that can wait, so the ordinary busy
	// path costs no extra HAL call.
	speakerBusy := isPassive && !h.agentGateway.IsBusy() &&
		speakergate.WaitsForSpeaker(req.Type) && speakergate.SpeakerBusy()
	// Typed chat, spoken input, and the silent realtime history sync may steer a
	// runtime that explicitly supports it. voice_agent_handled is deliberately
	// not isVoice: the realtime agent already answered aloud. When it arrives
	// during an active Codex turn, steering lets its own trace close on
	// bridge.steered instead of leaving a passive sync event queued behind work
	// that may take minutes. Keep the speaker gate intact for actual speech.
	steerer, supportsSteering := h.agentGateway.(domain.ActiveTurnSteerer)
	steerableInput := supportsSteering && steerer.SupportsActiveTurnSteering() && (isChat || isVoice || isRealtimeHandled)
	if isPassive && ((!steerableInput && h.agentGateway.IsBusy()) || speakerBusy) {
		// motion.activity and emotion.detected get queued (not dropped) because
		// HAL deduplicates both with a 5-min window at the source — if one
		// reaches the os-server it's genuinely new. Dropping it here would make HAL's
		// dedup think "sent" while the agent never saw the event, blocking the
		// next real transition for 5 min.
		if shouldQueueEvent(req.Type, req.Message, inVoiceWindow) {
			// Preserve voice metric correlation through queued replay as well
			// as chat acknowledgements. The queue carries the fixed run ID.
			var queuedRunID string
			if isChat || isVoice {
				_, queuedRunID = h.agentGateway.NextChatRunID()
				telemetry.ReportTaskStarted(req.Type, req.InteractionID, queuedRunID)
				if isChat {
					h.agentGateway.MarkWebChatRun(queuedRunID)
				}
			}
			busyReason := "agent busy, will replay on idle"
			if speakerBusy {
				busyReason = "speaker busy, will replay when the reply finishes"
			}
			slog.Info("INBOUND from HAL → QUEUED ("+busyReason+")",
				"component", "sensing",
				"backend", h.agentGateway.Name(),
				"type", req.Type,
				"runId", queuedRunID,
				"imageCount", len(req.Images),
				"inVoiceWindow", inVoiceWindow,
				"msgLen", len(req.Message),
				"message", req.Message)
			h.agentGateway.QueuePendingEvent(req.Type, req.Message, req.Images, queuedRunID)
			if speakerBusy {
				// Nothing else will drain this one: the drain normally rides on
				// a turn ending, and there is no turn in flight. Ask
				// speakergate to replay it once the speaker frees up.
				speakergate.DeferReplay(
					[]string{req.Type}, h.agentGateway.DrainPendingEvents,
				)
			}
			// A queued event consumes an agent turn on replay — counts
			// against the ambient floor like a live forward.
			h.lastAgentTurn.Store(time.Now().UnixMilli())
			resp := map[string]any{"handler": "queued"}
			if queuedRunID != "" {
				resp["runId"] = queuedRunID
			}
			if isRealtimeHandled {
				resp["speechSuppressed"] = speechSuppressed
			}
			c.JSON(http.StatusOK, serializers.ResponseSuccess(resp))
			return
		}
		slog.Info("INBOUND from HAL → DROPPED (agent busy, non-queueable type)",
			"component", "sensing", "backend", h.agentGateway.Name(), "type", req.Type)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "sensing_drop",
			Summary: "[" + req.Type + "] " + req.Message,
			Detail:  map[string]any{"type": req.Type, "reason": "agent_busy"},
		})
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{"handler": "dropped"}))
		return
	}

	// Guard mode: mark the run so SSE handler broadcasts the response via Telegram Bot API.
	guardActive := isPassive && h.config.GuardModeEnabled() && (req.Type == "presence.enter" || req.Type == "motion" || req.Type == "fire_hazard.detected")
	if guardActive {
		slog.Info("guard mode active", "component", "sensing", "type", req.Type)
	}

	// No local match — forward to OpenClaw agent
	if !h.agentGateway.IsReady() {
		notReadyRunID := fmt.Sprintf("not-ready-%d", time.Now().UnixMilli())
		req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, notReadyRunID)
		startPayload["interaction_id"] = req.InteractionID
		if taskGroup != "" {
			telemetry.ReportTaskExecution(notReadyRunID, req.InteractionID, "failed", "dispatch_error")
		}
		turnStart := flow.Start("sensing_input", startPayload, notReadyRunID)
		flow.End("sensing_input", turnStart, map[string]any{"error": "agent not connected"}, notReadyRunID)
		// Announce once via TTS so user knows the brain is restarting (cooldown 60s).
		if req.Type == "voice_command" || req.Type == "voice_followup" || req.Type == "presence.enter" {
			now := time.Now().UnixMilli()
			if last := h.lastNotReadyTTS.Load(); now-last > 60_000 {
				if h.lastNotReadyTTS.CompareAndSwap(last, now) {
					go func() {
						// SpeakCached: fixed phrase, self-caches into hal's WAV
						// cache — fires while the brain restarts, when a live
						// provider render is least reliable.
						if err := hal.SpeakCached(i18n.One(i18n.PhraseBrainRestart)); err != nil {
							slog.Warn("not-ready TTS failed", "component", "sensing", "error", err)
						}
					}()
				}
			}
		}
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("agent gateway not connected"))
		return
	}

	// Same run_id as chat.send / JSONL: SetTrace before flow.Start so enter matches this turn (not previous).
	reqID, runID := h.agentGateway.NextChatRunID()
	req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, runID)
	startPayload["interaction_id"] = req.InteractionID
	flow.SetTrace(runID)

	// Mark this run as guard-active so SSE handler broadcasts the agent response via Telegram.
	if guardActive {
		snap := extractSnapshotPath(req.Message)
		h.agentGateway.MarkGuardRun(runID, snap)
	}
	// The realtime voice agent already spoke this turn (voice_agent_handled): the
	// agent still processes it to absorb context (memory/mood), but its reply must
	// NOT be spoken again. Deterministic TTS suppress — the input-branching skill's
	// NO_REPLY is a soft backstop the LLM can ignore.
	if req.Type == "voice_agent_handled" {
		h.agentGateway.MarkSilentRun(runID)
	}
	// motion.activity events that fold in a posture nudge ship two extra
	// markers ([pose_bucket: ...] / [pose_worst: ...]). Stash them keyed
	// by runID so the SSE /dm path can attach the worst frames to the
	// Telegram message after the agent decides to nudge.
	//
	// Same trigger also writes a `posture_alert` row to the user's
	// posture JSONL — the habit skill's Flow A reads those rows to
	// derive peak_hour / side_bias / typical_risk. Without this bridge
	// the habit-skill posture extension stays starved (agent only logs
	// nudge/praise; the raw alert signal had no os-server-side writer).
	if req.Type == "motion.activity" {
		if bid, worst := extractPoseBucketMarkers(req.Message); bid != "" {
			h.agentGateway.MarkPoseBucketRun(runID, bid, worst)
			if alertUser := req.CurrentUser; alertUser != "" || mood.CurrentUser() != "" {
				if alertUser == "" {
					alertUser = mood.CurrentUser()
				}
				if extras, ok := extractPostureAlertExtras(req.Message); ok {
					posture.LogAlert(alertUser, extras)
				}
			}
		}
	}
	// Typed chat: suppress TTS — response displayed in the chat UI only.
	// Covers the MQTT chat.send path too: it forwards as type "mqtt_chat" unless
	// the caller asked to be spoken to (`speak: true`, which forwards as
	// "voice"), so a phone chatting from another room doesn't make the device
	// talk — and doesn't spend TTS on a reply nobody is in the room to hear.
	if isChat {
		h.agentGateway.MarkWebChatRun(runID)
	}
	// Important: pass explicit runID to flow.Start to avoid global trace race (another goroutine may interleave
	// between SetTrace() and Start()).
	turnStart := flow.Start("sensing_input", startPayload, runID)

	// Resolve user attribution: prefer the request payload, fall back to mood.
	// The drain path (service_events.go) snapshots this at queue time; here we
	// resolve fresh per request.
	currentUser := req.CurrentUser
	if currentUser == "" {
		currentUser = mood.CurrentUser()
	}
	// Guard tag is only built on the live path — the queue path always passes
	// "" because guard state isn't preserved across the queue.
	var guardTag string
	if guardActive {
		guardTag = "[sensing:" + req.Type + "][guard-active]"
		if inst := h.config.GuardInstruction; inst != "" {
			guardTag += "[guard-instruction: " + inst + "]"
		}
	}
	msg := sensingmsg.Build(req.Type, req.Message, currentUser, guardTag)

	// Strip [snapshot: ...] markers from the outgoing LLM message. The full text
	// (with snapshot paths) remains in the sensing_input JSONL via startPayload so
	// the Monitor UI can still render thumbnails — the agent just doesn't waste
	// tokens on the path.
	msg = reSnapshotPath.ReplaceAllString(msg, "")
	// Same treatment for the pose bucket markers — file paths are infra,
	// not LLM context. The Monitor UI reads them from the JSONL payload.
	msg = rePoseBucketMarker.ReplaceAllString(msg, "")
	msg = rePoseWorstMarker.ReplaceAllString(msg, "")
	msg = strings.ReplaceAll(msg, "\n\n\n", "\n\n")
	msg = strings.TrimSpace(msg)
	// harness-use copies this private routing context into its local request so
	// the final Harness recap can answer this exact turn without an extra device
	// agent rewrite. It is meaningful only to that skill.
	if isVoice || isChat {
		channel := "voice"
		if isChat {
			channel = "web"
		}
		msg += h.harnessRoutingContext(req.Message, runID, channel)
	}

	// Mark voice turns so the SSE handler can re-arm a Continuation filler
	// at each tool.end. Done before forwarding so the lifecycle.start
	// event can never race ahead of the mark.
	// Other turn types (passive sensing, web chat, guard) deliberately
	// stay unmarked — fillers are voice-only.
	//
	// Opening filler is fired-and-forget IMMEDIATELY here (not via
	// FillerManager timer). This is the pre-2026-05-04 behaviour: filler
	// arrives at hal ~5-10s before the LLM real reply, so it has time
	// to synthesize and play out before the real reply arrives — avoiding
	// the hal-side speak() lock-timeout=2s race that the timer-based
	// fire-at-lifecycle.start+FillerDelay path triggers.
	//
	// A turn the realtime model delegated (`[voice-instruction]`, see HAL
	// turn_dispatch) already got a filler from that model. Acknowledging it
	// again here promises an answer twice — and when the main agent then
	// decides NO_REPLY (correct for an unclear utterance) the user is left
	// waiting on a promise nobody keeps. Such turns get no opening filler
	// and only start filling at the first tool boundary.
	if isVoice {
		hal.StartVoiceFollowup(followupInteractionID, runID)
		if strings.HasPrefix(req.Message, realtimeDelegationPrefix) {
			DefaultFillerManager.MarkDelegatedVoiceRun(runID, req.InteractionID)
		} else {
			DefaultFillerManager.MarkVoiceRun(runID, req.InteractionID)
			// Owned by the utterance HAL is tracking, not by the run id: this
			// fires now, while HAL is still waiting for the response that would
			// tell it which run this turn became.
			go PlayOpeningFillerNow(fillerOwner(req.InteractionID, runID))
		}
	}

	var err error
	// Web monitor chat starting with "/" is a slash command — forward via
	// chat.send with deliver:false so OpenClaw routes the reply back to the
	// web client only (matches gw web). Without this, slash replies can be
	// swallowed by bound-channel routing and the SSE stream times out.
	isSlashCommand := isChat && strings.HasPrefix(msg, "/")
	// motion.activity: snapshot saved for UI but NOT sent to agent (save tokens — action name is enough)
	hasImage := len(req.Images) > 0 && req.Type != "motion.activity"

	// Unified entry-point log — every inbound message reaching the agent goes
	// through one of the INBOUND lines so `grep INBOUND` shows a complete
	// trail across all sources (HAL, channels, system).
	sourceLabel := "HAL"
	if isChat {
		sourceLabel = "WebMonitor"
	}
	slog.Info("INBOUND from "+sourceLabel+" → agent",
		"component", "sensing",
		"backend", h.agentGateway.Name(),
		"type", req.Type,
		"runId", runID,
		"reqId", reqID,
		"hasImage", hasImage,
		"imageCount", len(req.Images),
		"imageBytes", totalBase64Len(req.Images),
		"isSlash", isSlashCommand,
		"isChat", isChat,
		"isVoice", isVoice,
		"msgLen", len(msg),
		"message", msg)

	// Note: when the describe-first gate above converted the image to an
	// [image description] line, req.Images is empty and this turn goes down
	// the plain-text path. An image here means either a vision-capable main
	// model (raw attachment is correct) or a describe failure (degraded
	// fallback).
	if hasImage {
		if isSlashCommand {
			_, err = h.agentGateway.SendSlashCommandWithImagesAndRun(msg, req.Images, reqID, runID)
		} else {
			_, err = h.agentGateway.SendChatMessageWithImagesAndRun(msg, req.Images, reqID, runID)
		}
	} else {
		if isSlashCommand {
			_, err = h.agentGateway.SendSlashCommandWithRun(msg, reqID, runID)
		} else {
			_, err = h.agentGateway.SendChatMessageWithRun(msg, reqID, runID)
		}
	}

	if err != nil {
		if taskGroup != "" {
			telemetry.ReportTaskExecution(runID, req.InteractionID, "failed", "dispatch_error")
		}
		// Forward failed — drop the voice mark so we don't keep state
		// for a run that will never produce a lifecycle.start.
		hal.EndVoiceFollowup(runID)
		DefaultFillerManager.Cancel(runID)
		slog.Error("failed to send event", "component", "sensing", "error", err)
		flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()})
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	h.lastAgentTurn.Store(time.Now().UnixMilli())
	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
	flow.Log("agent_call", map[string]any{"type": req.Type, "run_id": runID}, runID)

	slog.Info("flow correlation", "op", "hal_agent_out", "section", "hal_to_openclaw",
		"device_run_id", runID, "sensing_type", req.Type,
		"note", "OpenClaw lifecycle UUID maps to device_run_id on lifecycle_start in SSE handler")
	slog.Info("event forwarded", "component", "sensing", "type", req.Type, "imageCount", len(req.Images), "runId", runID)
	resp := map[string]any{"runId": runID}
	if isRealtimeHandled {
		// Whether the older turn really lost the speaker — the only thing that
		// makes this a suppression situation worth measuring.
		resp["speechSuppressed"] = speechSuppressed
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(resp))
}

// MonitorEventRequest is the payload for pushing an event to the monitor bus.
type MonitorEventRequest struct {
	Type    string         `json:"type" validate:"required"`
	Summary string         `json:"summary" validate:"required"`
	Detail  map[string]any `json:"detail,omitempty"`
	RunID   string         `json:"runId,omitempty"`
}

// PostMonitorEvent allows internal services (e.g. HAL) to push events to the monitor bus.
func (h *SensingHandler) PostMonitorEvent(c *gin.Context) {
	var req MonitorEventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    req.Type,
		Summary: req.Summary,
		Detail:  req.Detail,
		RunID:   req.RunID,
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// EnableGuardRequest is the optional payload for enabling guard mode.
type EnableGuardRequest struct {
	Instruction string `json:"instruction,omitempty"`
}

// EnableGuard activates guard mode with an optional custom instruction.
func (h *SensingHandler) EnableGuard(c *gin.Context) {
	var req EnableGuardRequest
	// Body is optional — ignore bind errors (empty body is fine).
	_ = c.ShouldBindJSON(&req)

	t := true
	h.config.GuardMode = &t
	h.config.GuardInstruction = req.Instruction
	if err := h.config.Save(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("guard mode enabled", "component", "sensing", "instruction", req.Instruction)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"guard_mode":  true,
		"instruction": req.Instruction,
	}))
}

// DisableGuard deactivates guard mode and clears any custom instruction.
func (h *SensingHandler) DisableGuard(c *gin.Context) {
	f := false
	h.config.GuardMode = &f
	h.config.GuardInstruction = ""
	if err := h.config.Save(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	slog.Info("guard mode disabled", "component", "sensing")
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]bool{"guard_mode": false}))
}

// GetGuardStatus returns the current guard mode state.
func (h *SensingHandler) GetGuardStatus(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]bool{
		"guard_mode": h.config.GuardModeEnabled(),
	}))
}

// totalBase64Len is the combined base64 length of every attached image, for the
// INBOUND log line. Kept separate from the count so an oversized single photo
// and many small ones stay distinguishable in the log.
func totalBase64Len(images []string) int {
	n := 0
	for _, img := range images {
		n += len(img)
	}
	return n
}

// GuardAlertRequest is the payload for manually triggering a guard broadcast.
type GuardAlertRequest struct {
	Message string `json:"message" validate:"required"`
	Image   string `json:"image,omitempty"`
}

// PostGuardAlert broadcasts an alert message to all chat sessions (manual alerts only).
func (h *SensingHandler) PostGuardAlert(c *gin.Context) {
	var req GuardAlertRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	var imagePath string
	if req.Image != "" {
		if data, err := base64.StdEncoding.DecodeString(req.Image); err == nil {
			tmp := filepath.Join(os.TempDir(), fmt.Sprintf("guard-alert-%d.jpg", time.Now().UnixMilli()))
			if err := os.WriteFile(tmp, data, 0644); err == nil {
				imagePath = tmp
				defer os.Remove(tmp)
			}
		}
	}
	if err := h.agentGateway.Broadcast(req.Message, imagePath); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// GetSnapshot serves a sensing snapshot image.
// HAL writes snapshots as <dir>/<category>/<name>, where <category> is
// sensing_<prefix> (e.g. sensing_motion_activity) and <name> is <ms>.jpg.
// Checks persistent dir first (/var/lib/hal/snapshots/), falls back to tmp.
func (h *SensingHandler) GetSnapshot(c *gin.Context) {
	category := c.Param("category")
	name := c.Param("name")
	validCategory := strings.HasPrefix(category, "sensing_") ||
		strings.HasPrefix(category, "emotion_") ||
		strings.HasPrefix(category, "motion_")
	if !validCategory || strings.ContainsAny(category, "/\\") || strings.Contains(category, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	if !strings.HasSuffix(name, ".jpg") || strings.ContainsAny(name, "/\\") || strings.Contains(name, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	persistPath := filepath.Join("/var/lib/hal/snapshots", category, name)
	if _, err := os.Stat(persistPath); err == nil {
		c.File(persistPath)
		return
	}
	for _, dir := range []string{
		"/tmp/hal-sensing-snapshots",
		"/tmp/hal-emotion-snapshots",
		"/tmp/hal-motion-snapshots",
	} {
		p := filepath.Join(dir, category, name)
		if _, err := os.Stat(p); err == nil {
			c.File(p)
			return
		}
	}
	c.Status(http.StatusNotFound)
}

// GetAgentSnapshot serves a saved GET /camera/snapshot image referenced by a
// Flow Monitor tool result. Only JPEGs in an approved runtime workspace or
// HAL snapshot directory are accepted; the raw filesystem path is never sent
// to the UI.
// agentSnapshotRuntimes is the allow-list of runtimes whose snapshot dirs may
// be served. The runtime segment comes from a URL, so an unlisted name must
// never reach the filesystem.
//
// THREE places carry this list and all three must agree, or a frame is written
// and then cannot be shown: hal/config.py `_AGENT_CONFIG_DIRS` decides where
// HAL writes, agent/delivery/http/camera_snapshot.go decides whether a URL is
// built, and this decides whether that URL is served. opencode was present in
// the first and absent from the other two, so every snapshot taken on it was
// saved to disk and silently dropped.
var agentSnapshotRuntimes = map[string]bool{
	"openclaw":   true,
	"hermes":     true,
	"picoclaw":   true,
	"codex":      true,
	"claudecode": true,
	"opencode":   true,
}

func (h *SensingHandler) GetAgentSnapshot(c *gin.Context) {
	runtime := c.Param("runtime")
	source := c.Param("source")
	name := c.Param("name")
	if !regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg)$`).MatchString(name) {
		c.Status(http.StatusNotFound)
		return
	}
	if !agentSnapshotRuntimes[runtime] {
		c.Status(http.StatusNotFound)
		return
	}
	// /root/.<runtime> on a board; off-device the developer's own install. Same
	// resolution HAL uses for HAL_SNAPSHOT_DIR, so the Monitor reads back the
	// exact directory HAL wrote the frame to.
	home := syspath.AgentRuntimeHome(runtime)
	var dir string
	switch source {
	case "workspace":
		dir = filepath.Join(home, "workspace")
	case "media-hal-snapshots":
		dir = filepath.Join(home, "media", "hal-snapshots")
	default:
		c.Status(http.StatusNotFound)
		return
	}
	path := filepath.Join(dir, name)
	if info, err := os.Stat(path); err == nil && !info.IsDir() {
		c.File(path)
		return
	}
	c.Status(http.StatusNotFound)
}

// speechEmotionAudioDirs are the on-Pi locations where the speech_emotion
// service writes its debug WAV clips (mirrors HAL_SPEECH_EMOTION_AUDIO_DIR
// default + a persistent fallback). GetAudio serves files by basename from
// these dirs only.
var speechEmotionAudioDirs = []string{
	"/var/lib/hal/speech-emotion",
	"/tmp/hal-speech-emotion",
}

// audioURLForPath maps a raw on-Pi WAV path (from SensingEventRequest.Audio)
// to a UI-servable URL, or "" when the path is empty / not a .wav. Only the
// basename is exposed so the full filesystem path never leaks to the UI.
func audioURLForPath(path string) string {
	if path == "" {
		return ""
	}
	name := filepath.Base(path)
	if !strings.HasSuffix(name, ".wav") {
		return ""
	}
	return "/api/sensing/audio/" + name
}

// GetAudio serves a speech_emotion debug WAV clip by basename. This is a
// debug-only affordance for the Flow Monitor UI; the audio is never sent to
// the LLM. The speech_emotion service names clips <ms>_<user>_<label>.wav with
// user/label sanitized to [a-zA-Z0-9_-], so a strict basename check suffices.
func (h *SensingHandler) GetAudio(c *gin.Context) {
	name := c.Param("name")
	if !strings.HasSuffix(name, ".wav") || strings.ContainsAny(name, "/\\") || strings.Contains(name, "..") {
		c.Status(http.StatusNotFound)
		return
	}
	for _, dir := range speechEmotionAudioDirs {
		p := filepath.Join(dir, name)
		if _, err := os.Stat(p); err == nil {
			c.File(p)
			return
		}
	}
	c.Status(http.StatusNotFound)
}

// MoodLogRequest is the payload for logging a user mood event.
//
// kind="signal" (default): raw evidence from one source. Source + Trigger required.
// kind="decision": agent-synthesized mood. BasedOn + Reasoning recommended;
//
//	Source defaults to "agent". Trigger is ignored.
type MoodLogRequest struct {
	Mood      string `json:"mood" validate:"required"`                        // happy, sad, stressed, tired, excited, etc.
	Kind      string `json:"kind" validate:"omitempty,oneof=signal decision"` // signal (default) or decision
	Source    string `json:"source"`                                          // signal: camera|voice|telegram|conversation. Required for signals.
	Trigger   string `json:"trigger"`                                         // signal: action/context. Required for signals.
	BasedOn   string `json:"based_on"`                                        // decision only: short summary of inputs
	Reasoning string `json:"reasoning"`                                       // decision only: why this mood
	User      string `json:"user"`                                            // optional: agent passes when it knows (e.g. Telegram sender)
}

// PostMoodLog records a mood signal or decision row to the user's history.
func (h *SensingHandler) PostMoodLog(c *gin.Context) {
	var req MoodLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	kind := req.Kind
	if kind == "" {
		kind = mood.KindSignal
	}
	if kind == mood.KindSignal && (strings.TrimSpace(req.Source) == "" || strings.TrimSpace(req.Trigger) == "") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("signal requires source and trigger"))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	evt := mood.Event{
		Kind:      kind,
		Mood:      req.Mood,
		Source:    req.Source,
		Trigger:   req.Trigger,
		BasedOn:   req.BasedOn,
		Reasoning: req.Reasoning,
	}
	if kind == mood.KindDecision {
		evt.Trigger = ""
		if evt.Source == "" {
			evt.Source = "agent"
		}
	}
	mood.LogEvent(user, evt)
	slog.Info("mood logged", "component", "mood", "user", user, "kind", kind, "mood", req.Mood, "source", evt.Source, "trigger", evt.Trigger, "based_on", evt.BasedOn)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user": user,
		"kind": kind,
		"mood": req.Mood,
	}))
}

// WellbeingLogRequest is the payload for logging a wellbeing activity.
// Accepted actions:
//   - Bucket names (agent writes from motion.activity hybrid output): drink, break, celebrate
//   - Raw Kinetics sedentary labels (agent writes verbatim from motion.activity):
//     using computer, writing, texting, reading, drawing, playing controller
//     (reading book + reading newspaper are collapsed to "reading" in HAL)
//   - Nudge records (agent writes after speaking): nudge_hydration, nudge_break
//   - Presence markers (backend writes internally): enter, leave
//
// The enum is intentionally permissive for `action` — validator only requires a
// non-empty, short string. The log is append-only; semantic checks (what counts
// as a reset point for hydration/break) live in the Wellbeing SKILL.
type WellbeingLogRequest struct {
	Action string `json:"action" validate:"required,max=64"`
	Notes  string `json:"notes"`
	User   string `json:"user"`
}

// PostWellbeingLog appends a wellbeing activity entry for the given user.
func (h *SensingHandler) PostWellbeingLog(c *gin.Context) {
	var req WellbeingLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	wellbeing.LogForUser(user, req.Action, req.Notes)
	slog.Info("wellbeing logged", "component", "wellbeing", "user", user, "action", req.Action, "notes", req.Notes)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user":   user,
		"action": req.Action,
	}))
}

// --- Posture History API ---

// PostureLogRequest is the JSON body the agent / HW marker dispatcher sends to
// /api/posture/log. `action` is one of the constants in skillcontext/posture (alert,
// nudge, praise); only the fields relevant to that action are expected.
type PostureLogRequest struct {
	Action     string `json:"action" validate:"required"`
	NudgeLevel int    `json:"nudge_level,omitempty"`
	Score      int    `json:"score,omitempty"`
	Risk       string `json:"risk,omitempty"`
	LeftScore  int    `json:"left_score,omitempty"`
	RightScore int    `json:"right_score,omitempty"`
	Notes      string `json:"notes,omitempty"`
	User       string `json:"user"`
}

// PostPostureLog appends a posture-history row. Dispatches to LogAlert /
// LogNudge / LogPraise depending on `action`; unknown actions return 400.
func (h *SensingHandler) PostPostureLog(c *gin.Context) {
	var req PostureLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := req.User
	if strings.TrimSpace(user) == "" {
		user = mood.CurrentUser()
	}
	user = usercanon.Resolve(user)

	switch req.Action {
	case posture.ActionAlert:
		posture.LogAlert(user, posture.AlertExtras{
			Score:      req.Score,
			Risk:       req.Risk,
			LeftScore:  req.LeftScore,
			RightScore: req.RightScore,
		})
	case posture.ActionNudge:
		posture.LogNudge(user, req.NudgeLevel, req.Notes)
	case posture.ActionPraise:
		posture.LogPraise(user, req.Notes)
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError("unknown posture action: "+req.Action))
		return
	}

	slog.Info("posture logged", "component", "posture", "user", user, "action", req.Action, "level", req.NudgeLevel)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]string{
		"user":   user,
		"action": req.Action,
	}))
}

// --- Guard helpers ---

// Trailing \n? so stripping [snapshot:...] from a multi-line message doesn't
// leave a blank line behind. Capture group ([^\]]+) is unchanged so
// extractSnapshotPath still pulls the file path via FindStringSubmatch.
var reSnapshotPath = regexp.MustCompile(`\[snapshot:\s*([^\]]+)\]\n?`)

// Pose bucket markers — emitted by hal motion.py when a posture nudge
// rides along on motion.activity. They reference a hal-side bucket
// dir + the pre-selected worst-snapshot filenames, NOT a base64 image
// payload, so they're cheap to keep in the JSONL.
var rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
var rePoseWorstMarker = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)

// Vision handoff hint from HAL turn_dispatch: `[vision-image] <path> (a photo
// was JUST captured ...)`. The path points inside the agent's media allow-list
// on purpose (image-tool access when the main model has vision). Once the
// describe gate converts the image to text, that path must NOT survive: the
// agent will happily `read` it, injecting an image block into the session
// history that 404s every later turn on a text-only routed model.
var reVisionImageHint = regexp.MustCompile(`\[vision-image\][^\n]*`)
var reVisionImagePath = regexp.MustCompile(`\[vision-image\]\s+(/[^\s)]+)`)

// removeVisionSnapshot deletes the snapshot file referenced by the message's
// [vision-image] hint, if any. Prefix-gated to the HAL snapshot dir so a
// crafted message can't make the server delete arbitrary files. Best-effort:
// failure is logged, never fails the turn.
func removeVisionSnapshot(message string) {
	m := reVisionImagePath.FindStringSubmatch(message)
	if m == nil || !strings.Contains(m[1], "/media/hal-snapshots/") {
		return
	}
	if err := os.Remove(m[1]); err != nil && !os.IsNotExist(err) {
		slog.Warn("vision snapshot cleanup failed",
			"component", "sensing", "path", m[1], "error", err)
	}
}

// extractSnapshotPath extracts the snapshot file path from a sensing message.
func extractSnapshotPath(message string) string {
	m := reSnapshotPath.FindStringSubmatch(message)
	if m == nil {
		return ""
	}
	return strings.TrimSpace(m[1])
}

// extractPostureSummaryJSON locates the [posture_summary: …] marker
// and returns just the JSON object body (without the marker brackets).
// posture_summary nests two levels deep (`latest_left.body_scores`)
// and also contains array literals (`skipped_joints:[]`), both of
// which a flat regex would mishandle — walk braces manually instead.
func extractPostureSummaryJSON(message string) string {
	const tag = "[posture_summary:"
	i := strings.Index(message, tag)
	if i < 0 {
		return ""
	}
	start := strings.IndexByte(message[i:], '{')
	if start < 0 {
		return ""
	}
	start += i
	depth := 0
	for j := start; j < len(message); j++ {
		switch message[j] {
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				return message[start : j+1]
			}
		}
	}
	return ""
}

// riskLevelLabel maps the perception-service RULA risk_level enum to the string
// vocabulary the habit skill expects on posture_alert rows.
func riskLevelLabel(level int) string {
	switch level {
	case 4:
		return "high"
	case 3:
		return "medium"
	case 2:
		return "low"
	case 1:
		return "negligible"
	default:
		return ""
	}
}

// extractPostureAlertExtras parses the [posture_summary: ...] JSON
// payload on a motion.activity message and translates the fields the
// habit skill needs onto a posture.AlertExtras.
//
// Source-of-truth fields are the `worst_*` keys (hal pre-computes
// them as the max across the same 3 samples it surfaces for the DM
// gallery), so habit numbers stay aligned with the photos the user
// just saw. Falls back to `latest_*` when an older hal build is
// still in the field, so a half-rolled deploy doesn't drop alert
// rows entirely.
//
// Returns ok=false when neither set of fields carries score/risk —
// habit can tolerate sparse history but shouldn't see rows missing
// both pieces of ergonomic data.
func extractPostureAlertExtras(message string) (posture.AlertExtras, bool) {
	body := extractPostureSummaryJSON(message)
	if body == "" {
		return posture.AlertExtras{}, false
	}
	var s struct {
		WorstScore      int `json:"worst_score"`
		WorstRiskLevel  int `json:"worst_risk_level"`
		WorstLeftScore  int `json:"worst_left_score"`
		WorstRightScore int `json:"worst_right_score"`

		LatestScore     int `json:"latest_score"`
		LatestRiskLevel int `json:"latest_risk_level"`
		LatestLeft      struct {
			Score int `json:"score"`
		} `json:"latest_left"`
		LatestRight struct {
			Score int `json:"score"`
		} `json:"latest_right"`
	}
	if err := json.Unmarshal([]byte(body), &s); err != nil {
		return posture.AlertExtras{}, false
	}

	score := s.WorstScore
	risk := s.WorstRiskLevel
	left := s.WorstLeftScore
	right := s.WorstRightScore
	if score == 0 && risk == 0 {
		// Older hal builds (or any path where the worst aggregate
		// wasn't computed) — fall back to the last-sample values so
		// the alert row still lands.
		score = s.LatestScore
		risk = s.LatestRiskLevel
		left = s.LatestLeft.Score
		right = s.LatestRight.Score
	}
	if score == 0 && risk == 0 {
		return posture.AlertExtras{}, false
	}
	return posture.AlertExtras{
		Score:      score,
		Risk:       riskLevelLabel(risk),
		LeftScore:  left,
		RightScore: right,
	}, true
}

// extractPoseBucketMarkers pulls (bucket_id, [worst filenames]) from a
// motion.activity message. Returns empty bucket_id when the markers are
// absent (most motion.activity turns — no posture nudge folded in).
func extractPoseBucketMarkers(message string) (string, []string) {
	bm := rePoseBucketMarker.FindStringSubmatch(message)
	if bm == nil {
		return "", nil
	}
	bucketID := strings.TrimSpace(bm[1])
	if bucketID == "" {
		return "", nil
	}
	wm := rePoseWorstMarker.FindStringSubmatch(message)
	var worst []string
	if wm != nil {
		for _, part := range strings.Split(wm[1], ",") {
			part = strings.TrimSpace(part)
			if part != "" {
				worst = append(worst, part)
			}
		}
	}
	return bucketID, worst
}

// --- Music Suggestion History API ---

type MusicSuggestionLogRequest struct {
	User    string `json:"user" validate:"required"`
	Trigger string `json:"trigger" validate:"required"`
	Query   string `json:"query"`
	Message string `json:"message" validate:"required"`
}

// PostMusicSuggestionLog records a music suggestion event.
func (h *SensingHandler) PostMusicSuggestionLog(c *gin.Context) {
	var req MusicSuggestionLogRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := usercanon.Resolve(req.User)
	seq := musicsuggestion.Log(user, req.Trigger, req.Query, req.Message)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"user": user,
		"seq":  seq,
		"day":  time.Now().Format("2006-01-02"),
	}))
}

type MusicSuggestionStatusRequest struct {
	User   string `json:"user" validate:"required"`
	Day    string `json:"day" validate:"required"`
	Seq    int64  `json:"seq" validate:"required"`
	Status string `json:"status" validate:"required,oneof=accepted rejected expired"`
}

// PostMusicSuggestionStatus updates the status of a previously logged music suggestion.
func (h *SensingHandler) PostMusicSuggestionStatus(c *gin.Context) {
	var req MusicSuggestionStatusRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	user := usercanon.Resolve(req.User)
	ok := musicsuggestion.UpdateStatus(user, req.Day, req.Seq, req.Status)
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("music suggestion not found"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
}

// ambientFloorTypes are the passive sensing event types subject to the global
// cross-type turn floor (config.SensingTurnFloorSeconds). Everything here is
// ambient/advisory — each emitter re-offers on its own heartbeat, so dropping
// one occurrence only delays awareness. User-initiated types (voice, voice_command,
// voice_followup,
// voice_agent_handled, web_chat, mqtt_chat, touch.head_pat), safety (fire_hazard.detected),
// and presence enter/leave (greeting UX + session bookkeeping) are deliberately
// NOT floored.
var ambientFloorTypes = map[string]bool{
	"environment.update":      true,
	"motion.activity":         true,
	"emotion.detected":        true,
	"speech_emotion.detected": true,
	"sound":                   true,
	"presence.away":           true,
	"light.level":             true,
}

// shouldQueueEvent returns true if this sensing event type should be queued
// (not dropped) when the agent is busy.
func shouldQueueEvent(eventType, message string, inVoiceWindow bool) bool {
	// Distinct session keys preserve parallel Buddy completions in the pending queue.
	if strings.HasPrefix(eventType, "buddy.agent.") || strings.HasPrefix(eventType, "harness.agent.") {
		return true
	}

	switch eventType {
	case "presence.enter", "presence.leave", "voice",
		// voice_agent_handled carries the [HANDLED]/[REPLY] sync for a
		// conversation the realtime agent already spoke. It must never be
		// dropped: the exchange is real and the main agent's memory depends on
		// it. It used to fall to `default: inVoiceWindow`, which is effectively
		// always false — the 10s window opened by voice_listening has long
		// expired by the time a realtime turn finishes (measured ~21s), and
		// HAL sends voice_listening_end AFTER dispatch. The drain path has
		// always been ready for it (service_events.go re-applies MarkSilentRun
		// on replay); that branch was simply unreachable.
		"voice_agent_handled",
		"motion.activity", "emotion.detected", "speech_emotion.detected", "environment.update",
		"fire_hazard.detected",
		"web_chat", "mqtt_chat":
		return true
	case "sound":
		return strings.Contains(message, "persistent")
	default:
		return inVoiceWindow
	}
}

// VoiceFileRemoveRequest deletes ONE voice sample file from a user's
// /root/local/users/<name>/voice/ folder. Used by the Voice Enroll UI's
// per-file delete button. The sample's embedding sidecar (.npy) goes with it;
// because a speaker is a bank of independent per-sample rows, that is the
// whole operation — no re-enroll, no recompute. If no WAVs remain we POST
// /speaker/remove to drop the whole profile.
type VoiceFileRemoveRequest struct {
	Name string `json:"name" validate:"required"`
	File string `json:"file" validate:"required"`
}

const usersDir = "/root/local/users"

func (h *SensingHandler) RemoveVoiceFile(c *gin.Context) {
	var req VoiceFileRemoveRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	name := strings.ToLower(strings.TrimSpace(req.Name))
	file := strings.TrimSpace(req.File)
	// Path traversal guard — file must be a bare filename, no separators
	// or ".." components. The voice dir layout is flat.
	if name == "" || file == "" || strings.ContainsAny(file, "/\\") || file == "." || file == ".." {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid name or file"))
		return
	}
	// Only allow deleting audio samples. Each sample WAV owns a sibling .npy
	// holding its embedding, and the two are managed as a pair (deleting the
	// WAV below removes the sidecar with it) — letting the UI delete a .npy on
	// its own would strip a sample from the bank while leaving its audio
	// behind. metadata.json is profile state and must never be deleted here.
	// UI hides Delete for these too; this is the belt-and-braces guard.
	switch strings.ToLower(filepath.Ext(file)) {
	case ".wav", ".ogg", ".mp3", ".webm", ".m4a":
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError("only audio samples can be deleted"))
		return
	}

	voiceDir := filepath.Join(usersDir, name, "voice")
	target := filepath.Join(voiceDir, file)
	// Belt-and-braces: resolved path must stay under voiceDir.
	absVoice, err1 := filepath.Abs(voiceDir)
	absTarget, err2 := filepath.Abs(target)
	if err1 != nil || err2 != nil || !strings.HasPrefix(absTarget+string(filepath.Separator), absVoice+string(filepath.Separator)) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid path"))
		return
	}
	if _, err := os.Stat(target); err != nil {
		c.JSON(http.StatusNotFound, serializers.ResponseError("file not found"))
		return
	}
	if err := os.Remove(target); err != nil {
		slog.Warn("voice file remove failed", "component", "voice", "path", target, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("delete failed: "+err.Error()))
		return
	}
	// Remove the sample's embedding sidecar with it. The bank indexes by WAV,
	// so an orphaned .npy is invisible to matching — but it lingers in the
	// voice-file listing forever, and the guard above (correctly) refuses to
	// let the UI delete a .npy directly, so there would be no way to clear it.
	sidecar := strings.TrimSuffix(target, filepath.Ext(target)) + ".npy"
	if err := os.Remove(sidecar); err != nil && !os.IsNotExist(err) {
		slog.Warn("voice sidecar remove failed", "component", "voice", "path", sidecar, "error", err)
	}
	slog.Info("voice file deleted", "component", "voice", "name", name, "file", file)

	// Find remaining WAVs (only WAV files matter to speaker_recognizer).
	entries, _ := os.ReadDir(voiceDir)
	remainingWavs := []string{}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if strings.HasSuffix(strings.ToLower(e.Name()), ".wav") {
			remainingWavs = append(remainingWavs, filepath.Join(voiceDir, e.Name()))
		}
	}

	// No WAVs left → remove the speaker profile entirely so list endpoints
	// don't show a phantom user with 0 samples.
	if len(remainingWavs) == 0 {
		body, _ := json.Marshal(map[string]any{"name": name})
		resp, err := http.Post("http://127.0.0.1:5001/speaker/remove", "application/json", bytes.NewReader(body))
		if err != nil {
			slog.Warn("speaker/remove call failed", "component", "voice", "error", err)
		} else {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
			"deleted": file,
			"profile": "removed",
		}))
		return
	}

	// Nothing else to do. A speaker is stored as a BANK of per-sample
	// embeddings — one independent row per WAV — so removing the WAV and its
	// sidecar removes exactly that row and leaves every other row correct.
	//
	// This used to re-POST the remaining WAVs to /speaker/enroll, which was
	// necessary under the old model: the profile was a single aggregated
	// vector that still carried the deleted sample's contribution, so the only
	// way to drop it was to recompute from scratch. Against the bank that call
	// is actively harmful — enroll treats already-stored files as new input and
	// writes fresh copies beside them, so deleting 1 of 3 samples left 4. Every
	// duplicate is another max-over-rows chance for an impostor to score high,
	// which is exactly what the bank's sample caps exist to bound.
	//
	// HAL reads the bank straight off disk and derives its sample counts the
	// same way, so both matching and the UI are correct with no further call.
	slog.Info("voice file deleted", "component", "voice", "name", name,
		"file", file, "remaining", len(remainingWavs))
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"deleted":   file,
		"remaining": len(remainingWavs),
	}))
}
