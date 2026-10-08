package hermes

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/statusled"
)

const healthPollInterval = 10 * time.Second

// StartWS — naming carried over from the domain.AgentGateway interface for parity with openclaw, but under Hermes there is no persistent WebSocket.
func (s *HermesService) StartWS(ctx context.Context, handler domain.AgentEventHandler) {
	s.steeringMu.Lock()
	s.runtimeCtx = ctx
	s.steeringMu.Unlock()
	s.handlerMu.Lock()
	s.handler = handler
	s.handlerMu.Unlock()

	go s.runHealthLoop(ctx)
	<-ctx.Done()
}

// runHealthLoop polls /health on healthPollInterval until ctx is done.
func (s *HermesService) runHealthLoop(ctx context.Context) {
	tick := time.NewTicker(healthPollInterval)
	defer tick.Stop()
	s.probeHealth(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			s.probeHealth(ctx)
		}
	}
}

// probeHealth issues GET /health and updates connection state. /health/detailed is fetched on transition to ready=true so we can capture uptime_s if Hermes publishes it.
func (s *HermesService) probeHealth(ctx context.Context) {
	url := strings.TrimRight(BaseURL, "/") + "/health"
	probeCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, url, nil)
	if err != nil {
		s.transitionReady(false)
		return
	}
	if APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+APIKey)
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		slog.Debug("hermes health probe failed", "component", "hermes", "error", err)
		s.transitionReady(false)
		return
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()
	ok := resp.StatusCode >= 200 && resp.StatusCode < 300
	if !ok {
		slog.Warn("hermes health non-2xx", "component", "hermes", "status", resp.StatusCode)
	}
	if ok {
		// Upgrade only between executions.
		s.steeringMu.Lock()
		if !s.nativeRunSteering.Load() && s.inFlightStreams.Load() == 0 {
			s.nativeRunSteering.Store(s.discoverRunSteering(probeCtx))
		}
		s.steeringMu.Unlock()
	}
	s.transitionReady(ok)
	if ok {
		s.maybeFetchUptime(probeCtx)
	}
}

// transitionReady applies the new readiness state, updates statusled, and emits the one-shot reconnect TTS on re-up.
func (s *HermesService) transitionReady(now bool) {
	was := s.ready.Swap(now)
	if now == was {
		return
	}
	if now {
		defer s.drainPendingEvents()
		s.connectedAt.Store(time.Now().Unix())
		flow.Log("ws_ready", map[string]any{"backend": "hermes"})
		slog.Info("Hermes ready",
			"component", "hermes",
			"base_url", BaseURL,
			"conversation", s.conversationName(),
			"model", Model)
		if s.statusLED != nil && s.config.SetUpCompleted {
			s.statusLED.Clear(statusled.StateAgentDown)
		}
		if announce := s.reconnectNotice.Up(); s.hasConnected.Swap(true) && announce {
			go func() {
				phrase := i18n.Pick(i18n.PhraseReconnect)
				// SpeakCached: system filler must not enter realtime voice history.
				if err := hal.SpeakCached(phrase); err != nil {
					slog.Warn("reconnect TTS failed", "component", "hermes", "error", err)
				}
			}()
		}
	} else {
		s.connectedAt.Store(0)
		s.reconnectNotice.Down()
		flow.Log("ws_down", map[string]any{"backend": "hermes"})
		slog.Warn("Hermes unreachable", "component", "hermes", "base_url", BaseURL)
		if s.statusLED != nil && s.config.SetUpCompleted {
			s.statusLED.Set(statusled.StateAgentDown)
		}
	}
}

// maybeFetchUptime hits /health/detailed and captures uptime_s if present.
func (s *HermesService) maybeFetchUptime(ctx context.Context) {
	if s.agentStartedAt.Load() > 0 {
		return
	}
	url := strings.TrimRight(BaseURL, "/") + "/health/detailed"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return
	}
	if APIKey != "" {
		req.Header.Set("Authorization", "Bearer "+APIKey)
	}
	resp, err := s.httpClient.Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return
	}
	var body struct {
		UptimeS  int64 `json:"uptime_s"`
		UptimeMs int64 `json:"uptime_ms"`
	}
	raw, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(raw, &body); err != nil {
		return
	}
	var uptime int64
	switch {
	case body.UptimeS > 0:
		uptime = body.UptimeS
	case body.UptimeMs > 0:
		uptime = body.UptimeMs / 1000
	}
	if uptime > 0 {
		s.agentStartedAt.Store(time.Now().Unix() - uptime)
		slog.Info("hermes agent uptime captured", "component", "hermes", "uptime_s", uptime)
	}
}
