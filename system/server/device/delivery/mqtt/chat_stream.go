package mqtthandler

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func isSilentReply(evt domain.MonitorEvent) bool {
	if evt.Type != "chat_response" {
		return false
	}
	s := strings.TrimSpace(strings.ToLower(evt.Summary))
	return s == "no_reply" || s == "[no reply]"
}

const (
	// deltaFlush is how long assistant_delta text is accumulated before being
	// sent.
	deltaFlush = 250 * time.Millisecond

	// runTTL bounds how long a run is mirrored when no terminal event arrives —
	// a crashed or abandoned turn must not leak a tracked run forever.
	runTTL = 10 * time.Minute

	// chatStreamQueue is the buffer between the bus subscription and the
	// publishing goroutine. Deep enough to ride out a broker hiccup mid-turn.
	chatStreamQueue = 256
)

// terminalEventTypes end a run's mirroring on their own, regardless of state:
// these Types never fire more than once per run.
var terminalEventTypes = map[string]bool{
	"no_reply": true,
}

// chatResponseTerminalStates are the chat_response states that end a run.
// Must match the web reducer (ChatSection.tsx).
var chatResponseTerminalStates = map[string]bool{
	"complete": true,
	"final":    true,
	"error":    true,
}

// isTerminalChatEvent reports whether evt ends its run's mirroring.
func isTerminalChatEvent(evt domain.MonitorEvent) bool {
	if evt.Type == "chat_response" {
		return chatResponseTerminalStates[evt.State]
	}
	return terminalEventTypes[evt.Type]
}

// trackedRun is one backend-started turn being mirrored.
type trackedRun struct {
	sessionID string
	started   time.Time
	// pending holds assistant_delta text not yet published, and lastEvt the
	// event it was carried on so the flush can reuse its shape.
	pending  strings.Builder
	lastEvt  domain.MonitorEvent
	hasDelta bool
}

// ChatStream mirrors monitor events of backend-started runs onto fd_channel.
type ChatStream struct {
	cfg     *config.Config
	factory *mqtt.Factory
	bus     *monitor.Bus

	// deliveryMu keeps replay, live events, and delta flushes in delivery order.
	deliveryMu sync.Mutex
	mu         sync.Mutex
	runs       map[string]*trackedRun
	captures   map[*chatCapture][]domain.MonitorEvent

	// client is the long-lived publisher, created lazily on the first event so a
	// device that never receives a chat.send pays nothing.
	client *mqtt.MQTT

	// publish is the transport, swapped in tests.
	publish func(payload []byte) error
}

// ProvideChatStream constructs the mirror. Start must be called to run it.
func ProvideChatStream(cfg *config.Config, factory *mqtt.Factory, bus *monitor.Bus) *ChatStream {
	s := &ChatStream{cfg: cfg, factory: factory, bus: bus, runs: map[string]*trackedRun{}}
	s.publish = s.publishMQTT
	return s
}

// chatCapture exists only while a chat.send is waiting for its sensing response.
// Untracked events stay local until the response identifies the exact owned run.
type chatCapture struct{ marker byte }

func (s *ChatStream) capture() func(runID, sessionID string) {
	// Use a non-zero-sized allocation so concurrent captures have unique keys.
	token := &chatCapture{}
	s.mu.Lock()
	if s.captures == nil {
		s.captures = make(map[*chatCapture][]domain.MonitorEvent)
	}
	s.captures[token] = nil
	s.mu.Unlock()
	var once sync.Once
	return func(runID, sessionID string) {
		once.Do(func() {
			s.deliveryMu.Lock()
			defer s.deliveryMu.Unlock()
			s.mu.Lock()
			events := s.captures[token]
			delete(s.captures, token)
			s.mu.Unlock()
			if runID == "" {
				return
			}
			s.Track(runID, sessionID)
			for _, evt := range events {
				if evt.RunID == runID {
					s.handleOrdered(evt)
				}
			}
		})
	}
}

// Track marks a run as backend-started, so its events are mirrored. Called by
// the chat.send handler the moment the sensing endpoint returns a run id.
func (s *ChatStream) Track(runID, sessionID string) {
	if runID == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.runs[runID] = &trackedRun{sessionID: sessionID, started: time.Now()}
	slog.Info("chat stream tracking run", "component", "mqtt-chat", "run_id", runID, "session_id", sessionID)
}

// Start subscribes to the monitor bus and mirrors matching events until ctx is
// done. Non-blocking: the loop runs in its own goroutine.
func (s *ChatStream) Start(ctx context.Context) {
	events, unsub := s.bus.Subscribe()
	queue := make(chan domain.MonitorEvent, chatStreamQueue)

	// Two goroutines on purpose: Bus.Push DROPS an event when a subscriber's
	// channel is full, so the receiving side must never block on a broker
	// round-trip.
	go func() {
		defer unsub()
		defer close(queue)
		for {
			select {
			case <-ctx.Done():
				return
			case evt, ok := <-events:
				if !ok {
					return
				}
				select {
				case queue <- evt:
				default:
					slog.Warn("chat stream queue full, dropping event",
						"component", "mqtt-chat", "type", evt.Type, "run_id", evt.RunID)
				}
			}
		}
	}()

	go s.publishLoop(ctx, queue)
}

func (s *ChatStream) publishLoop(ctx context.Context, queue <-chan domain.MonitorEvent) {
	ticker := time.NewTicker(deltaFlush)
	defer ticker.Stop()
	defer s.closeClient()

	for {
		select {
		case <-ctx.Done():
			return
		case evt, ok := <-queue:
			if !ok {
				return
			}
			s.handle(evt)
		case <-ticker.C:
			s.flushAll()
			s.sweep()
		}
	}
}

// handle mirrors one event. Non-delta events flush pending text first to keep
// ordering.
func (s *ChatStream) handle(evt domain.MonitorEvent) {
	s.deliveryMu.Lock()
	defer s.deliveryMu.Unlock()
	s.handleOrdered(evt)
}

func (s *ChatStream) handleOrdered(evt domain.MonitorEvent) {
	if evt.RunID == "" {
		return
	}

	s.mu.Lock()
	run, tracked := s.runs[evt.RunID]
	if !tracked {
		for token := range s.captures {
			s.captures[token] = append(s.captures[token], evt)
		}
		s.mu.Unlock()
		return
	}

	if evt.Type == "assistant_delta" {
		run.pending.WriteString(deltaText(evt))
		run.lastEvt = evt
		run.hasDelta = true
		s.mu.Unlock()
		return
	}

	flushed := s.takePending(run)
	sessionID := run.sessionID
	terminal := isTerminalChatEvent(evt)
	if terminal {
		delete(s.runs, evt.RunID)
	}
	s.mu.Unlock()
	if isSilentReply(evt) {
		if terminal {
			evt.Summary = ""
			evt.Detail = map[string]string{"role": "assistant", "message": ""}
			s.send(evt.RunID, sessionID, evt)
		}
		return
	}

	if flushed != nil {
		s.send(evt.RunID, sessionID, *flushed)
	}
	s.send(evt.RunID, sessionID, evt)

	if terminal {
		slog.Info("chat stream run done", "component", "mqtt-chat", "run_id", evt.RunID, "type", evt.Type)
	}
}

// takePending drains a run's accumulated delta text into one event, or nil when
// there is nothing buffered. Caller holds the lock.
func (s *ChatStream) takePending(run *trackedRun) *domain.MonitorEvent {
	if !run.hasDelta || run.pending.Len() == 0 {
		return nil
	}
	evt := run.lastEvt
	evt.ID = ""
	evt.Summary = run.pending.String()
	evt.Detail = map[string]any{"text": run.pending.String(), "coalesced": true}
	run.pending.Reset()
	run.hasDelta = false
	return &evt
}

func (s *ChatStream) flushAll() {
	s.deliveryMu.Lock()
	defer s.deliveryMu.Unlock()
	type pendingSend struct {
		runID     string
		sessionID string
		evt       domain.MonitorEvent
	}
	var out []pendingSend

	s.mu.Lock()
	for runID, run := range s.runs {
		if evt := s.takePending(run); evt != nil {
			out = append(out, pendingSend{runID: runID, sessionID: run.sessionID, evt: *evt})
		}
	}
	s.mu.Unlock()

	for _, p := range out {
		s.send(p.runID, p.sessionID, p.evt)
	}
}

// sweep drops runs that never produced a terminal event.
func (s *ChatStream) sweep() {
	cutoff := time.Now().Add(-runTTL)
	s.mu.Lock()
	defer s.mu.Unlock()
	for runID, run := range s.runs {
		if run.started.Before(cutoff) {
			delete(s.runs, runID)
			slog.Warn("chat stream run expired without a terminal event",
				"component", "mqtt-chat", "run_id", runID, "ttl", runTTL)
		}
	}
}

// deltaText pulls the text out of an assistant_delta event. The bus carries it
// on Summary; the flow_event shape nests it under detail.text.
func deltaText(evt domain.MonitorEvent) string {
	if evt.Summary != "" {
		return evt.Summary
	}
	if d, ok := evt.Detail.(map[string]any); ok {
		if t, ok := d["text"].(string); ok {
			return t
		}
	}
	return ""
}

func (s *ChatStream) send(runID, sessionID string, evt domain.MonitorEvent) {
	payload := domain.MQTTDataResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(s.cfg, "data", device.GetDeviceMac()),
		Kind:             domain.KindChatEvent,
		Status:           "success",
		Data: domain.MQTTChatEventData{
			RunID:     runID,
			SessionID: sessionID,
			Event:     evt,
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		slog.Error("chat event marshal failed", "component", "mqtt-chat", "error", err)
		return
	}
	if err := s.publish(body); err != nil {
		slog.Error("chat event publish failed",
			"component", "mqtt-chat", "channel", s.cfg.FDChannel, "run_id", runID, "error", err)
	}
}

// publishMQTT is the real transport: one long-lived client, QoS 1.
func (s *ChatStream) publishMQTT(body []byte) error {
	client, err := s.publisher()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), publishTimeout)
	defer cancel()
	if err := client.Publish(ctx, s.cfg.FDChannel, byte(1), body); err != nil {
		s.closeClient()
		return err
	}
	return nil
}

// publisher returns the long-lived client, connecting on first use.
func (s *ChatStream) publisher() (*mqtt.MQTT, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.client != nil {
		return s.client, nil
	}
	// Distinct client id: a shared id makes the broker evict the other client.
	client := s.factory.GetClient("device-" + s.cfg.DeviceID + "-chat")
	ctx, cancel := context.WithTimeout(context.Background(), publishTimeout)
	defer cancel()
	if err := client.Connect(ctx); err != nil {
		return nil, err
	}
	s.client = client
	return client, nil
}

func (s *ChatStream) closeClient() {
	s.mu.Lock()
	client := s.client
	s.client = nil
	s.mu.Unlock()
	if client != nil {
		client.Close()
	}
}
