package codex

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/statusled"
)

// This sentinel means no socket write was attempted; other send failures must never be replayed.
var errDisconnectedBeforeSend = errors.New("codex websocket not connected")

const (
	// reconnectBackoff is the fixed wait between reconnect attempts.
	reconnectBackoff = 5 * time.Second
	// readDeadline bounds how long the read loop blocks waiting for a frame.
	readDeadline = 90 * time.Second
	// pingInterval is how often we send an application-level ping so the server
	// keeps the connection warm and our readDeadline keeps getting fed.
	pingInterval = 25 * time.Second
)

// StartWS connects to the Codex WebSocket and runs the read loop, calling
// handler for each translated event.
func (s *CodexService) StartWS(ctx context.Context, handler domain.AgentEventHandler) {
	// Device-owned Telegram inbound: ONE poll goroutine for the whole gateway
	// lifetime (outside the reconnect loop, so it survives WS drops; ctx-bound
	// so it dies with the gateway).
	go s.startTelegramPoll(ctx)
	go s.startDiscordBot(ctx)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		err := s.runWSConn(ctx, handler)
		if ctx.Err() != nil {
			return
		}
		if s.statusLED != nil && s.config.SetUpCompleted {
			s.statusLED.Set(statusled.StateAgentDown)
		}
		if s.config.SetUpCompleted && device.Has(s.config.DeviceTypeOrDefault(), device.CapMotion) {
			if err := hal.StopServoTracking(); err != nil {
				slog.Warn("stop servo tracking on ws disconnect failed", "component", "codex", "error", err)
			}
		}
		if err != nil {
			slog.Warn("websocket disconnected, reconnecting", "component", "codex", "error", err, "backoff", reconnectBackoff)
			flow.Log("ws_disconnect", map[string]any{"error": err.Error(), "backoff_s": reconnectBackoff.Seconds()})
		} else {
			slog.Warn("websocket connection closed, reconnecting", "component", "codex", "backoff", reconnectBackoff)
			flow.Log("ws_disconnect", map[string]any{"reason": "closed", "backoff_s": reconnectBackoff.Seconds()})
		}
		if !sleepCtx(ctx, reconnectBackoff) {
			return
		}
	}
}

// runWSConn dials, marks the socket ready, then pumps inbound frames through the
// translator until the socket errors or ctx is cancelled.
func (s *CodexService) runWSConn(ctx context.Context, handler domain.AgentEventHandler) error {
	s.wsConnected.Store(false)
	s.reconnectNotice.Down()
	s.wsConnectedAt.Store(0)
	defer func() {
		s.wsConnected.Store(false)
		s.reconnectNotice.Down()
		s.wsConnectedAt.Store(0)
	}()
	// Clear busy on disconnect — the final frame may never arrive.
	defer s.activeTurn.Store(false)
	defer s.clearTurn()

	connStart := flow.Start("ws_connect", map[string]any{"url": WSURL})

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	header := http.Header{}
	header.Set("Authorization", "Bearer "+Token)
	conn, resp, err := dialer.DialContext(ctx, WSURL, header)
	if err != nil {
		if resp != nil {
			flow.End("ws_connect", connStart, map[string]any{"error": err.Error(), "status": resp.Status})
			return fmt.Errorf("dial %s: %w (status %s)", WSURL, err, resp.Status)
		}
		flow.End("ws_connect", connStart, map[string]any{"error": err.Error()})
		return fmt.Errorf("dial %s: %w", WSURL, err)
	}
	defer func() {
		s.wsMu.Lock()
		s.wsConn = nil
		s.wsMu.Unlock()
		conn.Close()
	}()

	s.wsMu.Lock()
	s.wsConn = conn
	s.wsMu.Unlock()
	s.wsConnected.Store(true)
	s.wsConnectedAt.Store(time.Now().Unix())
	if s.statusLED != nil && s.config.SetUpCompleted {
		s.statusLED.Clear(statusled.StateAgentDown)
	}
	flow.End("ws_connect", connStart, map[string]any{"connected": true})
	flow.Log("ws_ready", map[string]any{"backend": "codex"})
	slog.Info("Codex connected", "component", "codex", "url", WSURL)

	// SpeakCached, not SendToHALTTS: system filler must not enter realtime voice history.
	if announce := s.reconnectNotice.Up(); s.wsHasConnected.Swap(true) && announce {
		go func() {
			phrase := i18n.Pick(i18n.PhraseReconnect)
			if err := hal.SpeakCached(phrase); err != nil {
				slog.Warn("reconnect TTS failed", "component", "codex", "error", err)
			}
		}()
	}

	pingCtx, cancelPing := context.WithCancel(ctx)
	defer cancelPing()
	go s.keepAlive(pingCtx)

	dispatch := func(evt domain.WSEvent) {
		if handler == nil {
			return
		}
		if err := handler(ctx, evt); err != nil {
			slog.Error("ws handler error", "component", "codex", "event", evt.Event, "error", err)
		}
	}
	// Publish it for the paths that must end a turn from outside this loop (see
	// failStuckTurn).
	// Cleared on the way out so a dead connection's handler is never used to answer a later turn.
	s.wsDispatch.Store(dispatchFn(dispatch))
	defer s.wsDispatch.Store(dispatchFn(nil))
	// End the correlated turn before the disconnect cleanup clears its IDs.
	defer func() {
		if ctx.Err() == nil {
			s.failDisconnectedTurn(dispatch)
		}
	}()

	// Drain only locally buffered, unsent events; sent pendingRuns are never replayed.
	go s.drainPendingEvents()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		conn.SetReadDeadline(time.Now().Add(readDeadline))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}
		s.translateFrame(msg, dispatch)
	}
}

// keepAlive sends an application-level ping every pingInterval.
func (s *CodexService) keepAlive(ctx context.Context) {
	tick := time.NewTicker(pingInterval)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			if err := s.sendFrame(map[string]any{
				"type": "ping",
				"id":   fmt.Sprintf("ping-%d", s.reqCounter.Add(1)),
			}); err != nil {
				return
			}
		}
	}
}

// sendFrame marshals v and writes it to the WebSocket under wsMu.
func (s *CodexService) sendFrame(v any) error {
	body, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("marshal frame: %w", err)
	}
	s.wsMu.Lock()
	conn := s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		return errDisconnectedBeforeSend
	}
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		return fmt.Errorf("write frame: %w", err)
	}
	return nil
}

// sleepCtx sleeps for d or until ctx is cancelled.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
