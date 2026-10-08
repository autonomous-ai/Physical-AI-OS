package openclaw

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/statusled"
)

// StartWS connects to the gateway WebSocket and runs the read loop, calling handler for each event.
func (s *OpenclawService) StartWS(ctx context.Context, handler domain.AgentEventHandler) {
	backoff := 5 * time.Second
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
		// Safety reflex: the gateway link just dropped, so any in-flight servo object-tracking is now chasing a target it can no longer get vision updates for.
		if s.config.SetUpCompleted && device.Has(s.config.DeviceTypeOrDefault(), device.CapMotion) {
			if err := hal.StopServoTracking(); err != nil {
				slog.Warn("stop servo tracking on ws disconnect failed", "component", "openclaw", "error", err)
			}
		}
		if err != nil {
			slog.Warn("websocket disconnected, reconnecting", "component", "openclaw", "error", err, "backoff", backoff)
			flow.Log("ws_disconnect", map[string]any{"error": err.Error(), "backoff_s": backoff.Seconds()})
		} else {
			slog.Warn("websocket connection closed, reconnecting", "component", "openclaw", "backoff", backoff)
			flow.Log("ws_disconnect", map[string]any{"reason": "closed", "backoff_s": backoff.Seconds()})
		}
		if !sleepCtx(ctx, backoff) {
			return
		}
	}
}

func (s *OpenclawService) runWSConn(ctx context.Context, handler domain.AgentEventHandler) error {
	return s.runWSConnAt(ctx, handler, defaultGatewayWSURL)
}

func (s *OpenclawService) runWSConnAt(ctx context.Context, handler domain.AgentEventHandler, gatewayURL string) error {
	s.wsConnected.Store(false)
	s.reconnectNotice.Down()
	s.wsConnectedAt.Store(0)
	defer func() {
		s.wsConnected.Store(false)
		s.reconnectNotice.Down()
		s.wsConnectedAt.Store(0)
	}()
	defer s.activeTurn.Store(false) // clear busy on disconnect — lifecycle_end may never arrive

	connStart := flow.Start("ws_connect", map[string]any{"url": gatewayURL})

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, resp, err := dialer.DialContext(ctx, gatewayURL, http.Header{})
	if err != nil {
		if resp != nil {
			flow.End("ws_connect", connStart, map[string]any{"error": err.Error(), "status": resp.Status})
			return fmt.Errorf("dial %s: %w (status %s)", gatewayURL, err, resp.Status)
		}
		flow.End("ws_connect", connStart, map[string]any{"error": err.Error()})
		return fmt.Errorf("dial %s: %w", gatewayURL, err)
	}
	defer func() {
		s.wsMu.Lock()
		s.wsConn = nil
		s.wsMu.Unlock()
		conn.Close()
	}()

	conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	_, msg, err := conn.ReadMessage()
	if err != nil {
		return fmt.Errorf("read connect.challenge: %w", err)
	}
	conn.SetReadDeadline(time.Time{})
	slog.Debug("initial event received", "component", "openclaw", "event", string(msg))

	// Parse nonce from connect.challenge
	var challenge struct {
		Payload struct {
			Nonce string `json:"nonce"`
		} `json:"payload"`
	}
	if err := json.Unmarshal(msg, &challenge); err != nil || challenge.Payload.Nonce == "" {
		return fmt.Errorf("parse connect.challenge nonce: %w", err)
	}
	nonce := challenge.Payload.Nonce

	token, err := s.readGatewayToken()
	if err != nil {
		flow.End("ws_connect", connStart, map[string]any{"error": "read token: " + err.Error()})
		return fmt.Errorf("read gateway token: %w", err)
	}

	di, err := s.loadOrCreateDeviceIdentity()
	if err != nil {
		return fmt.Errorf("device identity: %w", err)
	}

	signedAt := time.Now().UnixMilli()
	signature := di.signConnectPayload(token, nonce, signedAt)

	connectReq := map[string]interface{}{
		"type":   "req",
		"id":     "os-1",
		"method": "connect",
		"params": map[string]interface{}{
			"minProtocol": 3,
			"maxProtocol": 4,
			"client": map[string]interface{}{
				"id":       "node-host",
				"version":  "1.0",
				"platform": "linux",
				"mode":     "node",
			},
			"role":   "operator",
			"scopes": []string{"operator.read", "operator.write", "operator.admin"},
			"caps":   []string{"thinking-events", "tool-events"},
			"auth":   map[string]interface{}{"token": token},
			"device": map[string]interface{}{
				"id":        di.DeviceID,
				"publicKey": base64.StdEncoding.EncodeToString(di.PublicKey),
				"signature": signature,
				"signedAt":  signedAt,
				"nonce":     nonce,
			},
		},
	}
	connectBody, _ := json.Marshal(connectReq)
	if err := conn.WriteMessage(websocket.TextMessage, connectBody); err != nil {
		return fmt.Errorf("write connect: %w", err)
	}

	conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, connectResp, err := conn.ReadMessage()
	if err != nil {
		return fmt.Errorf("read connect response: %w", err)
	}
	conn.SetReadDeadline(time.Time{})
	slog.Debug("connect response", "component", "openclaw", "response", string(connectResp))

	// Check for pairing errors — if scope-upgrade, reset device identity and retry
	var connectErr struct {
		OK    bool `json:"ok"`
		Error struct {
			Code    string `json:"code"`
			Details struct {
				Reason string `json:"reason"`
			} `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(connectResp, &connectErr) == nil && !connectErr.OK && connectErr.Error.Code == "NOT_PAIRED" {
		reason := connectErr.Error.Details.Reason
		if reason == "scope-upgrade" || reason == "not-paired" {
			slog.Warn("pairing rejected, resetting device identity to re-pair",
				"component", "openclaw", "reason", reason)
			keyPath := filepath.Join(s.config.OpenclawConfigDir, deviceKeyFile)
			if err := os.Remove(keyPath); err != nil && !os.IsNotExist(err) {
				slog.Error("failed to remove device key", "component", "openclaw", "error", err)
			}
			return fmt.Errorf("pairing rejected (%s): identity reset, will re-pair on next connect", reason)
		}
		return fmt.Errorf("pairing rejected: %s", connectErr.Error.Code)
	}

	if !connectErr.OK {
		return fmt.Errorf("gateway connect rejected: %s", connectErr.Error.Code)
	}

	var connectResult struct {
		Type   string `json:"type"`
		Result struct {
			SessionKey string `json:"sessionKey"`
		} `json:"result"`
		Payload struct {
			// uptimeMs may live at payload top level (older gateway responses) or inside snapshot (current 2026.5.27 hello-ok shape) — capture both, prefer the non-zero one.
			UptimeMs int64 `json:"uptimeMs"`
			Snapshot struct {
				UptimeMs        int64 `json:"uptimeMs"`
				SessionDefaults struct {
					MainSessionKey string `json:"mainSessionKey"`
				} `json:"sessionDefaults"`
			} `json:"snapshot"`
		} `json:"payload"`
	}
	if err := json.Unmarshal(connectResp, &connectResult); err == nil {
		sk := connectResult.Result.SessionKey
		if sk == "" {
			sk = connectResult.Payload.Snapshot.SessionDefaults.MainSessionKey
		}
		if sk != "" {
			s.SetSessionKey(sk)
			slog.Info("session key from connect", "component", "openclaw", "sessionKey", sk)
		}
		upMs := connectResult.Payload.UptimeMs
		if upMs <= 0 {
			upMs = connectResult.Payload.Snapshot.UptimeMs
		}
		if upMs > 0 {
			s.agentStartedAt.Store(time.Now().Unix() - upMs/1000)
		}
	}

	if s.GetSessionKey() == "" {
		listReq := map[string]interface{}{
			"type":   "req",
			"id":     "os-sessions",
			"method": "sessions.list",
		}
		listBody, _ := json.Marshal(listReq)
		if err := conn.WriteMessage(websocket.TextMessage, listBody); err == nil {
			conn.SetReadDeadline(time.Now().Add(5 * time.Second))
			_, listResp, err := conn.ReadMessage()
			conn.SetReadDeadline(time.Time{})
			if err == nil {
				slog.Debug("sessions.list response", "component", "openclaw", "response", string(listResp))
				var listResult struct {
					Result struct {
						Sessions []struct {
							SessionKey string `json:"sessionKey"`
						} `json:"sessions"`
					} `json:"result"`
				}
				if json.Unmarshal(listResp, &listResult) == nil && len(listResult.Result.Sessions) > 0 {
					sk := listResult.Result.Sessions[0].SessionKey
					s.SetSessionKey(sk)
					slog.Info("session key from sessions.list", "component", "openclaw", "sessionKey", sk)
				}
			}
		}
	}

	s.wsMu.Lock()
	s.wsConn = conn
	s.wsMu.Unlock()
	s.wsConnected.Store(true)
	s.wsConnectedAt.Store(time.Now().Unix())
	if s.statusLED != nil && s.config.SetUpCompleted {
		s.statusLED.Clear(statusled.StateAgentDown)
	}
	flow.End("ws_connect", connStart, map[string]any{"session_key": s.GetSessionKey() != ""})
	flow.Log("ws_ready", map[string]any{"session": s.GetSessionKey() != ""})

	// On reconnect (not first boot), announce via TTS so user knows agent is back.
	if announce := s.reconnectNotice.Up(); s.wsHasConnected.Swap(true) && announce {
		go func() {
			phrase := i18n.Pick(i18n.PhraseReconnect)
			if err := hal.SpeakCached(phrase); err != nil {
				slog.Warn("reconnect TTS failed", "component", "openclaw", "error", err)
			}
		}()
	}

	subReq := map[string]interface{}{
		"type":   "req",
		"id":     fmt.Sprintf("sub-%d", s.reqCounter.Add(1)),
		"method": "sessions.subscribe",
		"params": map[string]interface{}{},
	}
	if body, err := json.Marshal(subReq); err == nil {
		s.wsMu.Lock()
		_ = conn.WriteMessage(websocket.TextMessage, body)
		s.wsMu.Unlock()
		slog.Info("sessions.subscribe sent", "component", "openclaw")
	}

	// Event handler runs in a worker goroutine so it does not block the read loop.
	const eventChanBuf = 64
	eventCh := make(chan domain.WSEvent, eventChanBuf)
	handlerDone := make(chan struct{})
	go func() {
		defer close(handlerDone)
		defer func() {
			if r := recover(); r != nil {
				slog.Error("ws handler panic — worker exiting", "component", "openclaw", "panic", r)
			}
		}()
		for evt := range eventCh {
			if handler == nil {
				continue
			}
			if err := handler(ctx, evt); err != nil {
				slog.Error("ws handler error", "component", "openclaw", "event", evt.Event, "error", err)
			}
		}
	}()
	defer func() {
		close(eventCh)
		<-handlerDone
	}()

	// The authenticated socket and event worker are ready.
	go s.drainPendingEvents()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		_, msg, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		if s.GetSessionKey() == "" {
			var raw struct {
				SessionKey string `json:"sessionKey"`
				Result     struct {
					SessionKey string `json:"sessionKey"`
				} `json:"result"`
				Payload json.RawMessage `json:"payload"`
			}
			if json.Unmarshal(msg, &raw) == nil {
				sk := raw.SessionKey
				if sk == "" {
					sk = raw.Result.SessionKey
				}
				if sk == "" && len(raw.Payload) > 0 {
					var p struct {
						SessionKey string `json:"sessionKey"`
					}
					if json.Unmarshal(raw.Payload, &p) == nil {
						sk = p.SessionKey
					}
				}
				if sk != "" {
					s.SetSessionKey(sk)
				}
			}
		}

		// Must run before pushing to eventCh: handler RPCs block on pendingRPC channels delivered only here.
		s.dispatchRPCResponse(msg)

		var evt domain.WSEvent
		if err := json.Unmarshal(msg, &evt); err != nil {
			continue
		}
		select {
		case eventCh <- evt:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// dispatchRPCResponse checks if msg is an RPC response and delivers it to the waiting caller.
func (s *OpenclawService) dispatchRPCResponse(msg []byte) {
	var frame struct {
		Type    string          `json:"type"`
		ID      string          `json:"id"`
		OK      bool            `json:"ok"`
		Payload json.RawMessage `json:"payload"`
	}
	if json.Unmarshal(msg, &frame) != nil || frame.Type != "res" || frame.ID == "" {
		return
	}
	s.pendingRPCMu.Lock()
	ch, ok := s.pendingRPC[frame.ID]
	if ok {
		delete(s.pendingRPC, frame.ID)
	}
	s.pendingRPCMu.Unlock()
	if ok {
		select {
		case ch <- frame.Payload:
		default:
		}
	}
}

// FetchChatHistory sends a chat.history RPC and returns the raw payload.
func (s *OpenclawService) FetchChatHistory(sessionKey string, limit int) (json.RawMessage, error) {
	s.wsMu.Lock()
	conn := s.wsConn
	s.wsMu.Unlock()
	if conn == nil {
		return nil, fmt.Errorf("websocket not connected")
	}
	if sessionKey == "" {
		sessionKey = s.GetSessionKey()
	}
	if sessionKey == "" {
		return nil, fmt.Errorf("no session key")
	}

	reqID := fmt.Sprintf("history-%d", s.reqCounter.Add(1))
	ch := make(chan json.RawMessage, 1)

	s.pendingRPCMu.Lock()
	s.pendingRPC[reqID] = ch
	s.pendingRPCMu.Unlock()

	req := map[string]interface{}{
		"type":   "req",
		"id":     reqID,
		"method": "chat.history",
		"params": map[string]interface{}{
			"sessionKey": sessionKey,
			"limit":      limit,
		},
	}
	body, err := json.Marshal(req)
	if err != nil {
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("marshal chat.history: %w", err)
	}

	s.wsMu.Lock()
	conn = s.wsConn
	if conn == nil {
		s.wsMu.Unlock()
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("websocket disconnected before send")
	}
	err = conn.WriteMessage(websocket.TextMessage, body)
	s.wsMu.Unlock()
	if err != nil {
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("write chat.history: %w", err)
	}

	timer := time.NewTimer(3 * time.Second)
	defer timer.Stop()
	select {
	case payload := <-ch:
		return payload, nil
	case <-timer.C:
		s.pendingRPCMu.Lock()
		delete(s.pendingRPC, reqID)
		s.pendingRPCMu.Unlock()
		return nil, fmt.Errorf("chat.history timeout")
	}
}

func (s *OpenclawService) readGatewayToken() (string, error) {
	path := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	var cfg struct {
		Gateway struct {
			Auth struct {
				Token string `json:"token"`
			} `json:"auth"`
		} `json:"gateway"`
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return "", err
	}
	token := cfg.Gateway.Auth.Token
	if token == "" {
		return "", fmt.Errorf("gateway.auth.token is empty in %s", path)
	}
	return token, nil
}
