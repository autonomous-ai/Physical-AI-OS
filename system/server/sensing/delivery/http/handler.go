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

// realtimeDelegationPrefix opens the message HAL sends when the realtime
// model hands a turn to the main agent.
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
	// Images are optional base64-encoded JPEG snapshots.
	Images []string `json:"images,omitempty"`
	// InteractionID correlates task metrics.
	InteractionID string `json:"interaction_id,omitempty"`
	// CurrentUser is HAL's view of who is effectively in front of the device
	// right now (from FaceRecognizer.current_user()).
	CurrentUser string `json:"current_user,omitempty"`
	// Audio is an optional path (on the Pi) to the WAV clip that produced
	// this event — currently only speech_emotion.detected, carrying the
	// latest clip of the dominant label this flush.
	Audio string `json:"audio,omitempty"`
	// File is an optional NON-IMAGE attachment from a chat client (a PDF, a
	// CSV).
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
	// the agent handler mutes the older turn still in flight.
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

// SetOnRealtimeHandled installs the realtime-handled hook.
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
	intent.Configure(device.Capabilities(cfg.DeviceTypeOrDefault()))
	sensingmsg.SetEnvironmentReplayAllowed(func() bool {
		return cfg.EnvironmentSettings().Enabled && device.Capabilities(cfg.DeviceTypeOrDefault())[device.CapEnvironment] &&
			(isSleeping == nil || !isSleeping())
	})
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
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxSensingBodyBytes)
	var req SensingEventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := validator.New().Struct(req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	if err := validateEventAttachments(req); err != nil {
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
	// voice_listening / voice_listening_end are internal LED signals —
	// don't forward to agent.
	if req.Type == "voice_listening" {
		h.voiceActiveUntil.Store(time.Now().Add(10 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}
	if req.Type == "voice_listening_end" {
		h.voiceActiveUntil.Store(time.Now().Add(5 * time.Second).UnixMilli())
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}

	// Only HAL-supplied interaction IDs can own follow-up focus.
	followupInteractionID := req.InteractionID
	taskGroup := telemetry.TaskGroup(req.Type)
	if taskGroup == "voice" || taskGroup == "chat" {
		req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, "")
	}
	startPayload := map[string]any{"type": req.Type, "message": req.Message, "interaction_id": req.InteractionID}
	if kind := req.voiceTurnType(); kind != "" {
		startPayload["voice_turn_type"] = kind
	}

	// look.capture is monitor-only: the frame already reached the model, so
	// forwarding would inject a phantom turn.
	if req.Type == "look.capture" {
		lookRunID := fmt.Sprintf("look-%d", time.Now().UnixMilli())
		lookStart := flow.Start("sensing_input", startPayload, lookRunID)
		flow.End("sensing_input", lookStart, map[string]any{"type": req.Type}, lookRunID)
		c.JSON(http.StatusOK, serializers.ResponseSuccess(nil))
		return
	}

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

	// speech_emotion.detected is exempt: SER identifies nobody, so its
	// "unknown" must not overwrite a face-derived current user.
	if req.CurrentUser != "" && req.Type != "speech_emotion.detected" {
		mood.SetCurrentUser(req.CurrentUser)
	} else if req.Type == "presence.leave" || req.Type == "presence.away" {
		mood.ClearCurrentUser()
	}

	if h.harnessVoice != nil && h.harnessVoice(c, req) {
		return
	}

	isVoice := req.Type == "voice" || req.Type == "voice_command" || req.Type == "voice_followup"
	isChat := sensingmsg.IsChat(req.Type)
	if (isVoice || isChat) && len(req.Images) == 0 && len(req.Files) == 0 && h.config.LocalIntentEnabled() && !h.deferContextualIntent(req.Message) {
		if result := h.matchVoiceIntent(c.Request.Context(), req.Message); result != nil {
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
					// owner: a locally-handled command is answered here and
					// never gets a run id, so without it the reply the user
					// actually hears would be unattributed audio.
					if err := hal.SpeakCachedForTurn(result.TTSText, owner); err != nil {
						slog.Warn("intent TTS failed", "component", "sensing", "error", err)
					}
				}()
			}
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
				// Keep runId absent: MQTT correlates its final event via localRunId.
				"handledLocally": "true",
			}))
			return
		}
	}

	// Sleep guard: while the agent is in "sleepy" state, drop all passive
	// sensing (light.level, motion, sound) so they don't wake the agent and
	// override the sleepy emotion.
	isVoiceCommand := req.Type == "voice_command" || req.Type == "voice_followup"
	// Realtime-handled turns bypass the sleep drop (the user was already
	// answered) and stay out of isVoice so no opening filler plays.
	isRealtimeHandled := req.Type == "voice_agent_handled"
	// Must run BEFORE the busy fork: a busy agent queues voice_agent_handled
	// and returns early, exactly when an older turn is still in flight.
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

	// Global cross-type floor for ambient turns. Placed BEFORE the describe
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

	// Voice wake: when a voice command arrives while sleeping, fire greeting
	// emotion to HAL so it wakes up (LED + servo) before the agent processes
	// the turn.
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

	// Save chat images BEFORE the busy fork so a queued turn carries the tag.
	if isChat {
		for i, img := range req.Images {
			if img == "" {
				continue
			}
			imgData, derr := base64.StdEncoding.DecodeString(img)
			if derr != nil {
				continue
			}
			tmpPath := fmt.Sprintf("/tmp/web-chat-%d-%d.jpg", time.Now().UnixMilli(), i)
			if werr := os.WriteFile(tmpPath, imgData, 0644); werr == nil {
				req.Message += "\n[image: " + tmpPath + "]"
			}
		}
	}

	// Non-image files get their own branch: they must skip the describe-first
	// gate below, which keys off req.Images.
	for i, f := range req.Files {
		if f.Content == "" {
			continue
		}
		path, ferr := agentfile.SaveInbound("/tmp", f.Name, f.Content, time.Now().UnixMilli()+int64(i))
		if ferr != nil {
			slog.Warn("chat attachment not saved", "component", "sensing",
				"name", f.Name, "error", ferr)
			continue
		}
		name := strings.TrimSpace(f.Name)
		if name == "" {
			name = filepath.Base(path)
		}
		req.Message += fmt.Sprintf("\n[file: %s (%s)]", path, name)
	}

	// Describe-first gate — BEFORE the busy fork: queued replays send raw
	// attachments with no gate of their own.
	if len(req.Images) > 0 && req.Type != "motion.activity" &&
		!(isChat && strings.HasPrefix(strings.TrimSpace(req.Message), "/")) &&
		!vision.ModelSupportsVision(h.config) {
		// CONCURRENT, not sequential: this runs inside the HTTP handler, so
		// the caller's POST does not return until every describe finishes.
		results := make([]string, len(req.Images))
		errs := make([]error, len(req.Images))
		var wg sync.WaitGroup
		for i, img := range req.Images {
			if img == "" {
				continue
			}
			wg.Add(1)
			// safego, not a bare goroutine: a panic inside describe (a
			// malformed response body has done it) must not take the whole
			// os-server down on a user-attached photo.
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
		if len(descs) > 0 {
			derr = nil
		}
		// Either way delete the snapshot: it sits in the agent's media
		// allow-list and could later be read into an image block.
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
			// Drop the snapshot path from the hint so the agent cannot read the
			// image (it would poison text-only session history).
			req.Message = reVisionImageHint.ReplaceAllString(req.Message,
				"[vision-image] (a photo was just captured for this request; answer the visual question from the [image description] below — do NOT take a new snapshot, do NOT read any image file)")
			req.Message += "\n[image description] " + desc
		}
		req.Images = nil // text-only from here on; nothing downstream gets the blobs
	}

	// When busy: authorized voice passes through; voice/presence queue; other
	// passive sensing queues in the voice window, otherwise drops.
	inVoiceWindow := time.Now().UnixMilli() < h.voiceActiveUntil.Load()
	// An idle runtime may still be speaking its reply; wait for the speaker.
	speakerBusy := isPassive && !h.agentGateway.IsBusy() &&
		speakergate.WaitsForSpeaker(req.Type) && speakergate.SpeakerBusy()
	steerer, supportsSteering := h.agentGateway.(domain.ActiveTurnSteerer)
	steerableInput := supportsSteering && steerer.SupportsActiveTurnSteering() && (isChat || isVoice || isRealtimeHandled)
	if isPassive && ((!steerableInput && h.agentGateway.IsBusy()) || speakerBusy) {
		// HAL dedups some events at source; dropping one here would block
		// the next real transition for the dedup window.
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
				speakergate.DeferReplay(
					[]string{req.Type}, h.agentGateway.DrainPendingEvents,
				)
			}
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

	guardActive := isPassive && h.config.GuardModeEnabled() && (req.Type == "presence.enter" || req.Type == "motion" || req.Type == "fire_hazard.detected")
	if guardActive {
		slog.Info("guard mode active", "component", "sensing", "type", req.Type)
	}

	if !h.agentGateway.IsReady() {
		notReadyRunID := fmt.Sprintf("not-ready-%d", time.Now().UnixMilli())
		req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, notReadyRunID)
		startPayload["interaction_id"] = req.InteractionID
		if taskGroup != "" {
			telemetry.ReportTaskExecution(notReadyRunID, req.InteractionID, "failed", "dispatch_error")
		}
		turnStart := flow.Start("sensing_input", startPayload, notReadyRunID)
		flow.End("sensing_input", turnStart, map[string]any{"error": "agent not connected"}, notReadyRunID)
		if req.Type == "voice_command" || req.Type == "voice_followup" || req.Type == "presence.enter" {
			now := time.Now().UnixMilli()
			if last := h.lastNotReadyTTS.Load(); now-last > 60_000 {
				if h.lastNotReadyTTS.CompareAndSwap(last, now) {
					go func() {
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

	reqID, runID := h.agentGateway.NextChatRunID()
	req.InteractionID = telemetry.ReportTaskStarted(req.Type, req.InteractionID, runID)
	startPayload["interaction_id"] = req.InteractionID
	flow.SetTrace(runID)

	if guardActive {
		snap := extractSnapshotPath(req.Message)
		h.agentGateway.MarkGuardRun(runID, snap)
	}
	// The realtime voice agent already spoke this turn (voice_agent_handled):
	// the agent still processes it to absorb context (memory/mood), but its
	// reply must NOT be spoken again.
	if req.Type == "voice_agent_handled" {
		h.agentGateway.MarkSilentRun(runID)
	}
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
	if isChat {
		h.agentGateway.MarkWebChatRun(runID)
	}
	// Important: pass explicit runID to flow.Start to avoid global trace race (another goroutine may interleave
	// between SetTrace() and Start()).
	turnStart := flow.Start("sensing_input", startPayload, runID)

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

	msg = reSnapshotPath.ReplaceAllString(msg, "")
	msg = rePoseBucketMarker.ReplaceAllString(msg, "")
	msg = rePoseWorstMarker.ReplaceAllString(msg, "")
	msg = strings.ReplaceAll(msg, "\n\n\n", "\n\n")
	msg = strings.TrimSpace(msg)
	if isVoice || isChat {
		channel := "voice"
		if isChat {
			channel = "web"
		}
		msg += h.harnessRoutingContext(req.Message, runID, channel)
	}

	// Mark voice turns before forwarding so lifecycle.start cannot race the
	// mark. Delegated turns skip the opening filler (realtime already gave one).
	if isVoice {
		hal.StartVoiceFollowup(followupInteractionID, runID)
		if strings.HasPrefix(req.Message, realtimeDelegationPrefix) {
			DefaultFillerManager.MarkDelegatedVoiceRun(runID, req.InteractionID)
		} else {
			DefaultFillerManager.MarkVoiceRun(runID, req.InteractionID)
			go PlayOpeningFillerNow(fillerOwner(req.InteractionID, runID))
		}
	}

	var err error
	isSlashCommand := isChat && strings.HasPrefix(msg, "/")
	hasImage := len(req.Images) > 0 && req.Type != "motion.activity"

	sourceLabel := "HAL"
	switch req.Type {
	case "web_chat":
		sourceLabel = "WebMonitor"
	case "mqtt_chat":
		sourceLabel = "MobileApp"
	}
	slog.Info("INBOUND from "+sourceLabel+" → agent",
		"component", "sensing",
		"backend", h.agentGateway.Name(),
		"type", req.Type,
		// The agent gets the same "[user] " text for voice, monitor chat and
		// phone chat; this field is the only place they are told apart.
		"source", sensingmsg.TurnSource(req.Type, req.Message),
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

// totalBase64Len is the combined base64 length of every attached image, for
// the INBOUND log line.
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

// agentSnapshotRuntimes allow-lists runtimes whose snapshot dirs may be served.
// Keep in sync with hal/config.py _AGENT_CONFIG_DIRS and camera_snapshot.go.
var agentSnapshotRuntimes = map[string]bool{
	"openclaw":   true,
	"hermes":     true,
	"picoclaw":   true,
	"codex":      true,
	"claudecode": true,
	"opencode":   true,
}

// GetAgentSnapshot serves a saved GET /camera/snapshot image referenced by a
// Flow Monitor tool result; the raw filesystem path is never sent to the UI.
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
// default + a persistent fallback).
var speechEmotionAudioDirs = []string{
	"/var/lib/hal/speech-emotion",
	"/tmp/hal-speech-emotion",
}

// audioURLForPath maps a raw on-Pi WAV path (from SensingEventRequest.Audio)
// to a UI-servable URL, or "" when the path is empty / not a .wav.
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

// GetAudio serves a speech_emotion debug WAV clip by basename.
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

// PostureLogRequest is the JSON body the agent / HW marker dispatcher sends
// to /api/posture/log.
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

// Trailing \n? so stripping a marker leaves no blank line behind.
var reSnapshotPath = regexp.MustCompile(`\[snapshot:\s*([^\]]+)\]\n?`)

// Pose bucket markers — emitted by hal motion.py when a posture nudge rides
// along on motion.activity.
var rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
var rePoseWorstMarker = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)

// Vision handoff hint from HAL turn_dispatch: `[vision-image] <path> (a photo
// was JUST captured ...)`.
var reVisionImageHint = regexp.MustCompile(`\[vision-image\][^\n]*`)
var reVisionImagePath = regexp.MustCompile(`\[vision-image\]\s+(/[^\s)]+)`)

// removeVisionSnapshot deletes the snapshot file referenced by the message's
// [vision-image] hint, if any. Prefix-gated to the HAL snapshot dir.
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

// extractPostureSummaryJSON locates the [posture_summary: …] marker and
// returns just the JSON object body (without the marker brackets).
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

// extractPostureAlertExtras parses the [posture_summary: ...] JSON payload on
// a motion.activity message and translates the fields the habit skill needs
// onto a posture.AlertExtras.
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
// motion.activity message.
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
// cross-type turn floor (config.SensingTurnFloorSeconds).
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
	if strings.HasPrefix(eventType, "buddy.agent.") || strings.HasPrefix(eventType, "harness.agent.") {
		return true
	}

	switch eventType {
	case "presence.enter", "presence.leave", "voice",
		// voice_agent_handled must never be dropped: the main agent's memory
		// sync of a real exchange depends on it.
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
// /root/local/users/<name>/voice/ folder.
type VoiceFileRemoveRequest struct {
	Name string `json:"name" validate:"required"`
	File string `json:"file" validate:"required"`
}

const usersDir = "/root/local/users"

func (h *SensingHandler) RemoveVoiceFile(c *gin.Context) {
	h.removeVoiceFile(c, usersDir)
}

func (h *SensingHandler) removeVoiceFile(c *gin.Context, root string) {
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
	// Both profile and sample names must be single path components.
	if !voicePathComponent(name) || !voicePathComponent(file) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid name or file"))
		return
	}
	// Audio samples only: a .npy goes with its WAV, and metadata.json is
	// profile state.
	switch strings.ToLower(filepath.Ext(file)) {
	case ".wav", ".ogg", ".mp3", ".webm", ".m4a":
	default:
		c.JSON(http.StatusBadRequest, serializers.ResponseError("only audio samples can be deleted"))
		return
	}

	voiceDir := filepath.Join(root, name, "voice")
	target := filepath.Join(voiceDir, file)
	voiceRoot, err := openVoiceDirectory(root, name)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, serializers.ResponseError("file not found"))
		} else {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid voice directory"))
		}
		return
	}
	defer voiceRoot.Close()
	info, err := voiceRoot.Stat(file)
	if err != nil {
		if os.IsNotExist(err) {
			c.JSON(http.StatusNotFound, serializers.ResponseError("file not found"))
		} else {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid sample path"))
		}
		return
	}
	if !info.Mode().IsRegular() {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("sample must be a regular file"))
		return
	}
	if err := voiceRoot.Remove(file); err != nil {
		slog.Warn("voice file remove failed", "component", "voice", "path", target, "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("delete failed: "+err.Error()))
		return
	}
	// Remove the .npy sidecar too: the UI cannot delete a .npy directly, so an orphan would linger.
	sidecar := strings.TrimSuffix(file, filepath.Ext(file)) + ".npy"
	if err := voiceRoot.Remove(sidecar); err != nil && !os.IsNotExist(err) {
		slog.Warn("voice sidecar remove failed", "component", "voice", "path", sidecar, "error", err)
	}
	slog.Info("voice file deleted", "component", "voice", "name", name, "file", file)

	directory, err := voiceRoot.Open(".")
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot inspect remaining voice samples"))
		return
	}
	entries, err := directory.ReadDir(-1)
	directory.Close()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("cannot inspect remaining voice samples"))
		return
	}
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

	// No re-enroll: the bank is one row per WAV, and enroll would duplicate
	// the remaining samples.
	slog.Info("voice file deleted", "component", "voice", "name", name,
		"file", file, "remaining", len(remainingWavs))
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"deleted":   file,
		"remaining": len(remainingWavs),
	}))
}
