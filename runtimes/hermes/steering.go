package hermes

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

type managedChat struct {
	runID  string
	body   streamRequest
	source string
}
type managedStreamEvent struct {
	event   *domain.WSEvent
	result  streamResult
	pending string
	err     error
	done    bool
}
type managedTurn struct {
	id           string
	requests     []managedChat
	owner        string
	stopping     bool
	steered      bool
	conversation string
	terminalData map[string]any
	stopRunID    string
	streamed     map[string]string
	cancel       context.CancelFunc
	expireReason string
}

// SupportsNativeSteering is true only after the server advertises /runs control.
func (s *HermesService) SupportsNativeSteering() bool     { return s.nativeRunSteering.Load() }
func (s *HermesService) SupportsActiveTurnSteering() bool { return s.SupportsNativeSteering() }

var _ domain.ActiveTurnSteerer = (*HermesService)(nil)

func (s *HermesService) runContext() context.Context {
	s.steeringMu.Lock()
	defer s.steeringMu.Unlock()
	if s.runtimeCtx != nil {
		return s.runtimeCtx
	}
	return context.Background()
}

// enqueueManagedRun never waits on HTTP or a handler, including when a handler
// synchronously sends another request while processing a lifecycle event.
func (s *HermesService) enqueueManagedRun(runID string, body streamRequest, source string) {
	s.steeringMu.Lock()
	s.steeringQueue = append(s.steeringQueue, managedChat{runID, body, source})
	if s.steeringWake == nil {
		s.steeringWake = make(chan struct{}, 1)
		s.runExpiries = make(chan runExpiry)
		go s.managedLoop()
	}
	wake := s.steeringWake
	s.steeringMu.Unlock()
	select {
	case wake <- struct{}{}:
	default:
	}
}

func (s *HermesService) takeManagedQueue() []managedChat {
	s.steeringMu.Lock()
	defer s.steeringMu.Unlock()
	q := s.steeringQueue
	s.steeringQueue = nil
	return q
}

func managedUserInput(message, source string) bool {
	if source == "system" {
		return false
	}
	text := strings.TrimSpace(message)
	if strings.Contains(text, "[HANDLED]") {
		return false
	}
	for _, prefix := range []string{"[sensing:", "[activity]", "[emotion]", "[speech_emotion]", "[environment:", "[ambient signals"} {
		if strings.HasPrefix(text, prefix) {
			return false
		}
	}
	return text != ""
}

// Only explicit commands are controls. Natural language is not inferred to be
// a stop, and text following a stop token is an ordinary request.
var managedReplyRoute = regexp.MustCompile(`^\[harness-reply run_id=[^\s\[\]]+ channel=(?:web|voice)\]$`)

func managedStop(message string) bool {
	text := strings.TrimSpace(message)
	lines := strings.Split(text, "\n")
	command := strings.TrimSpace(lines[0])
	if command == "/stop" || command == "/interrupt" {
		// Typed slash commands retain their response route after queueing.
		// Only this exact metadata suffix may follow the standalone command.
		for _, line := range lines[1:] {
			line = strings.TrimSpace(line)
			if line != "" && !managedReplyRoute.MatchString(line) {
				return false
			}
		}
		return true
	}
	text = strings.TrimSpace(strings.TrimPrefix(text, "[user]"))
	text = strings.TrimSpace(strings.TrimPrefix(text, "[ambient]"))
	if !strings.HasPrefix(text, "[voice-instruction]") {
		return false
	}
	text = strings.TrimSpace(strings.TrimPrefix(text, "[voice-instruction]"))
	lines = strings.Split(text, "\n")
	for _, line := range lines[1:] {
		line = strings.TrimSpace(line)
		if line != "" && !strings.HasPrefix(line, "[transcript]") && !managedReplyRoute.MatchString(line) {
			return false
		}
	}
	text = strings.TrimSpace(lines[0])
	return strings.EqualFold(text, "stop") || strings.EqualFold(text, "stop.") || text == "/stop" || text == "/interrupt"
}

func (s *HermesService) managedAudible(request managedChat) bool {
	text, ok := request.body.Input.(string)
	return ok && managedUserInput(text, request.source) && !s.IsWebChatRun(request.runID) && !s.IsSilentRun(request.runID)
}

func (s *HermesService) managedDispatch(ctx context.Context, event domain.WSEvent) {
	if handler := s.currentHandler(); handler != nil {
		if err := handler(ctx, event); err != nil {
			slog.Error("hermes managed dispatch", "event", event.Event, "error", err)
		}
	}
}

func (s *HermesService) managedLifecycle(ctx context.Context, runID, phase, message string) {
	data := map[string]any{"phase": phase, "endedAt": nowUnixMs()}
	if phase == "start" {
		data = map[string]any{"phase": phase, "startedAt": nowUnixMs()}
	}
	if message != "" {
		data["error"] = message
	}
	payload, _ := json.Marshal(map[string]any{"runId": runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle", "data": data})
	s.managedDispatch(ctx, domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *HermesService) releaseManaged(request managedChat) {
	s.RemovePendingChatTraceByRunID(request.runID)
	if s.inFlightStreams.Add(-1) == 0 {
		s.SetBusy(false)
	}
}

func (s *HermesService) failManaged(ctx context.Context, request managedChat, err error) {
	s.managedLifecycle(ctx, request.runID, "error", err.Error())
	s.releaseManaged(request)
}

var managedHWMarker = regexp.MustCompile(`\[HW:((?:/[^{:\]]+(?::[^{:\]]+)*))(?::(\{[^}]*\}))?\]`)
var managedHWLink = regexp.MustCompile(`(?i)\[([^\]]*)\]\(\s*HW:\s*(?:/[^(){:\s]+(?::[^(){:\s]+)*)(?::\{[^}]*\})?:?\s*\)`)

func managedChildText(text string) string {
	text = managedHWLink.ReplaceAllStringFunc(text, func(match string) string {
		label := managedHWLink.FindStringSubmatch(match)[1]
		if strings.HasPrefix(strings.ToUpper(label), "HW:") {
			return ""
		}
		return label
	})
	return strings.TrimSpace(managedHWMarker.ReplaceAllString(text, ""))
}

// A final result is emitted once per device request, but only one copy carries
// hardware markers and only the newest audible request may speak.
func (s *HermesService) finishManaged(ctx context.Context, active *managedTurn, result streamResult, err error) {
	if active.expireReason != "" {
		err = errors.New(active.expireReason)
	}
	if err == nil && result.Errored {
		err = errors.New(result.ErrorText)
	}
	for _, request := range active.requests {
		if request.runID != active.owner && !s.IsWebChatRun(request.runID) {
			s.MarkSilentRun(request.runID)
		}
	}
	for _, request := range active.requests {
		if err == nil && !active.stopping {
			text := result.FinalText
			if request.runID != active.owner {
				text = managedChildText(text)
			}
			// The shared handler speaks from assistant deltas, not chat.final.
			// Deliver held text before lifecycle.end flushes that accumulator.
			// A normal progressive reply contributes only its missing suffix.
			streamed := active.streamed[request.runID]
			if strings.HasPrefix(text, streamed) {
				delta := strings.TrimPrefix(text, streamed)
				if delta != "" {
					payload, _ := json.Marshal(map[string]any{"runId": request.runID, "sessionKey": s.GetSessionKey(), "stream": "assistant", "data": map[string]any{"delta": delta}})
					s.managedDispatch(ctx, domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
				}
			}
			payload, _ := json.Marshal(map[string]any{"runId": request.runID, "sessionKey": s.GetSessionKey(), "state": "final", "role": "assistant", "message": text})
			s.managedDispatch(ctx, domain.WSEvent{Type: "evt", Event: "chat", Payload: payload})
		}
		phase, message := "end", ""
		if err != nil {
			phase, message = "error", err.Error()
		}
		if active.stopping && request.runID == active.stopRunID && result.Terminal {
			phase, message = "end", ""
		}
		data := map[string]any{"phase": phase, "endedAt": nowUnixMs()}
		if message != "" {
			data["error"] = message
		}
		if request.runID == active.requests[0].runID && active.terminalData != nil {
			if usage, ok := active.terminalData["usage"]; ok {
				data["usage"] = usage
			}
		}
		payload, _ := json.Marshal(map[string]any{"runId": request.runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle", "data": data})
		s.managedDispatch(ctx, domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
		s.releaseManaged(request)
	}
}

func managedPendingSuffix(requests []managedChat, pending string) int {
	// The server coalesces accepted steering strings with one newline. Match
	// original boundaries exactly; never reconstruct a request from model text.
	for i := 1; i < len(requests); i++ {
		texts := make([]string, 0, len(requests)-i)
		for _, request := range requests[i:] {
			text, _ := request.body.Input.(string)
			texts = append(texts, strings.TrimSpace(text))
		}
		if strings.Join(texts, "\n") == pending {
			return i
		}
	}
	return -1
}

const managedStopNotice = "[system-routing: The user explicitly stopped the preceding task. It is cancelled; do not resume it unless the user asks to resume it. The following is the new user request.]\n"

type managedStopContext struct{ conversation string }

func (state *managedStopContext) observe(active *managedTurn, result streamResult) {
	if active.stopping && active.expireReason == "" && result.Terminal {
		state.conversation = active.conversation
	}
}

func (state *managedStopContext) prepare(request managedChat) (streamRequest, bool) {
	body := request.body
	if state.conversation == "" {
		return body, false
	}
	if body.Conversation != state.conversation {
		state.conversation = ""
		return body, false
	}
	switch input := body.Input.(type) {
	case string:
		if !managedUserInput(input, request.source) {
			return body, false
		}
		body.Input = managedStopNotice + input
	case []inputMessage:
		if len(input) != 1 || input[0].Role != "user" || request.source == "system" {
			return body, false
		}
		var text strings.Builder
		for _, part := range input[0].Content {
			if part.Type == "input_text" || part.Type == "text" {
				text.WriteString(part.Text)
			}
		}
		if !managedUserInput(text.String(), request.source) {
			return body, false
		}
		// Copy both containers: the original request is retained unchanged for
		// device-run correlation and verified pending-steer replay.
		messages := append([]inputMessage(nil), input...)
		messages[0].Content = append([]inputContent{{Type: "input_text", Text: managedStopNotice}}, input[0].Content...)
		body.Input = messages
	default:
		return body, false
	}
	return body, true
}

func (s *HermesService) managedLoop() {
	ctx := s.runContext()
	events := make(chan managedStreamEvent, 64)
	var active *managedTurn
	var readerDone chan struct{}
	var waiting []managedChat
	uncertainConversation := ""
	var stopContext managedStopContext
	for {
		if active == nil && len(waiting) > 0 {
			request := waiting[0]
			waiting = waiting[1:]
			if ctx.Err() != nil {
				s.failManaged(context.Background(), request, ctx.Err())
				continue
			}
			if uncertainConversation != "" && request.body.Conversation == uncertainConversation {
				s.failManaged(ctx, request, errors.New("previous Hermes run has unknown acceptance or terminal state; start a new session before retrying"))
				continue
			}
			if text, ok := request.body.Input.(string); ok && managedStop(text) {
				s.managedLifecycle(ctx, request.runID, "start", "")
				s.managedLifecycle(ctx, request.runID, "end", "")
				s.releaseManaged(request)
				continue
			}
			// Image-bearing requests wait for idle, then use native Runs as
			// canonical multimodal content in the same Hermes session.
			s.steeringMu.Lock()
			session := s.managedSession
			if !s.managedSessionSet {
				session = s.GetSessionKey()
			}
			s.steeringMu.Unlock()
			wireBody, carriesStopContext := stopContext.prepare(request)
			createCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
			id, err := s.createManagedRun(createCtx, wireBody, session)
			cancel()
			if err != nil {
				var status *managedRunHTTPError
				// Only a reply lost AFTER the request went out is uncertain. A
				// dial failure (connection refused while hermes-gateway restarts —
				// lamp-0c4e 2026-09-16, presync config change restarted it exactly
				// as the skill-update turn was sent) means the prompt was never
				// delivered, so the conversation stays usable.
				var dial *net.OpError
				if !errors.As(err, &status) && !(errors.As(err, &dial) && dial.Op == "dial") {
					uncertainConversation = request.body.Conversation
					s.rotateConversation() // see the stream-loss branch below
				}
				s.failManaged(ctx, request, fmt.Errorf("create native Hermes run: %w", err))
				continue
			}
			if carriesStopContext {
				stopContext.conversation = ""
			}
			readerCtx, readerCancel := context.WithCancel(ctx)
			active = &managedTurn{cancel: readerCancel, id: id, requests: []managedChat{request}, owner: request.runID, conversation: request.body.Conversation}
			s.managedLifecycle(ctx, request.runID, "start", "")
			readerDone = make(chan struct{})
			go func(id, deviceID string, done chan struct{}) {
				defer close(done)
				result, pending, err := s.readManagedRun(readerCtx, id, deviceID, func(event domain.WSEvent) {
					select {
					case events <- managedStreamEvent{event: &event}:
					case <-ctx.Done():
					}
				})
				select {
				case events <- managedStreamEvent{result: result, pending: pending, err: err, done: true}:
				case <-ctx.Done():
				}
			}(id, request.runID, readerDone)
			// Requests accumulated during creation must get the same steering
			// admission as requests arriving after the first SSE frame.
			if len(waiting) > 0 {
				s.steeringMu.Lock()
				s.steeringQueue = append(waiting, s.steeringQueue...)
				waiting = nil
				s.steeringMu.Unlock()
				select {
				case s.steeringWake <- struct{}{}:
				default:
				}
			}
		}
		select {
		case <-ctx.Done():
			if active != nil {
				stopCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				_ = s.controlManagedRun(stopCtx, active.id, "stop", "")
				cancel()
				// The reader performs bounded remote stop/status cleanup on
				// cancellation. Join it before dropping the controller owner.
				<-readerDone
				s.finishManaged(context.Background(), active, streamResult{}, ctx.Err())
			}
			waiting = append(waiting, s.takeManagedQueue()...)
			for _, request := range waiting {
				s.failManaged(context.Background(), request, ctx.Err())
			}
			return
		case expiry := <-s.runExpiries:
			expiry.reply <- expireManagedOwner(active, expiry)
		case <-s.steeringWake:
			incoming := s.takeManagedQueue()
			for _, request := range incoming {
				text, textOnly := request.body.Input.(string)
				canControl := textOnly && managedUserInput(text, request.source) && (!s.IsSilentRun(request.runID) || s.IsWebChatRun(request.runID))
				if active == nil || !canControl || active.stopping || request.body.Conversation != active.conversation {
					waiting = append(waiting, request)
					continue
				}
				action := "steer"
				if managedStop(text) {
					action = "stop"
				}
				controlCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
				err := s.controlManagedRun(controlCtx, active.id, action, text)
				cancel()
				if err != nil {
					var status *managedRunHTTPError
					if errors.As(err, &status) && status.StatusCode == 409 {
						waiting = append(waiting, request)
						continue
					}
					// The server may have accepted a request whose HTTP reply was lost.
					// Surface the uncertainty and never send that prompt a second time.
					s.failManaged(ctx, request, fmt.Errorf("Hermes %s acceptance uncertain: %w", action, err))
					continue
				}
				active.requests = append(active.requests, request)
				active.steered = true
				if action == "stop" {
					active.stopping = true
					active.stopRunID = request.runID
					for _, old := range active.requests {
						s.MarkSilentRun(old.runID)
					}
				} else if s.managedAudible(request) {
					for _, old := range active.requests {
						if old.runID != request.runID && !s.IsWebChatRun(old.runID) {
							s.MarkSilentRun(old.runID)
						}
					}
					active.owner = request.runID
				}
				s.managedLifecycle(ctx, request.runID, "start", "")
				flow.Log("chat_steered", map[string]any{"run_id": request.runID, "managed_run": active.id, "action": action}, request.runID)
			}
		case update := <-events:
			if active == nil {
				continue
			}
			if !update.done {
				var payload map[string]any
				_ = json.Unmarshal(update.event.Payload, &payload)
				stream, _ := payload["stream"].(string)
				if stream == "lifecycle" {
					if data, ok := payload["data"].(map[string]any); ok && data["phase"] != "start" {
						active.terminalData = data
					}
				} else if stream == "tool" {
					s.managedDispatch(ctx, *update.event)
				} else if stream == "assistant" && !active.stopping && !active.steered {
					// Keep ordinary replies progressive. A steering ack is
					// not consumption evidence: hold subsequent output until
					// the terminal confirms whether the new input was used.
					if active.streamed == nil {
						active.streamed = make(map[string]string)
					}
					if data, ok := payload["data"].(map[string]any); ok {
						delta, _ := data["delta"].(string)
						active.streamed[active.owner] += delta
					}
					payload["runId"] = active.owner
					raw, _ := json.Marshal(payload)
					event := *update.event
					event.Payload = raw
					s.managedDispatch(ctx, event)
				}
				continue
			}
			if !update.result.Terminal {
				uncertainConversation = active.conversation
				if update.err == nil {
					update.err = errors.New("native Hermes stream ended without a terminal")
				}
				// Self-heal: the guard below refuses every later request on this
				// conversation because the lost run may still be acting on the
				// prompt, and nothing else ever rotates it — lamp-0c4e 2026-09-16:
				// hermes-gateway restarted mid-run (presync config change), then
				// every web/voice turn for 6+ minutes failed instantly with
				// "start a new session before retrying" until os-server was
				// restarted. Rotate now: requests already queued on the old
				// conversation still fail (never re-send an uncertain prompt), new
				// ones go to a fresh conversation the gateway has never seen.
				s.rotateConversation()
			}
			if update.result.Terminal {
				s.steeringMu.Lock()
				if s.conversationName() == active.conversation {
					s.managedSession = s.GetSessionKey()
					s.managedSessionSet = true
				}
				s.steeringMu.Unlock()
			}
			// An expired request must never be replayed, even if the remote
			// terminal reports it as an unconsumed steering suffix.
			if update.pending != "" && active.expireReason == "" {
				index := managedPendingSuffix(active.requests, update.pending)
				if index >= 0 && update.result.Terminal {
					waiting = append(append([]managedChat{}, active.requests[index:]...), waiting...)
					active.requests = active.requests[:index]
					// A replayed request still owns its pending trace and in-flight count.
					active.owner = active.requests[0].runID
				} else {
					update.err = errors.New("Hermes returned uncorrelated pending_steer; not replaying uncertain input")
				}
			}
			stopContext.observe(active, update.result)
			s.finishManaged(ctx, active, update.result, update.err)
			active.cancel()
			active = nil
		}
	}
}
