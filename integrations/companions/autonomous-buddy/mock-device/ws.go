package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	// Dev only: buddy connects from the same Mac.
	CheckOrigin: func(r *http.Request) bool { return true },
}

// HandleWS upgrades to WebSocket, validates the bearer token and routes responses to Dispatch waiters.
func (s *State) HandleWS(w http.ResponseWriter, r *http.Request) {
	auth := r.Header.Get("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		http.Error(w, "missing bearer", http.StatusUnauthorized)
		return
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	record := s.lookupByToken(token)
	if record == nil {
		http.Error(w, "invalid token", http.StatusUnauthorized)
		return
	}

	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		logf("WS upgrade error: %v", err)
		return
	}
	s.setWS(ws)
	logf("✓ buddy connected: %s", record.BuddyID)

	go s.greet(record.BuddyID)

	defer func() {
		s.clearWS()
		_ = ws.Close()
		logf("buddy disconnected")
	}()

	ws.SetPongHandler(func(string) error { return nil })

	for {
		_, data, err := ws.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				logf("WS read error: %v", err)
			}
			return
		}
		var env struct {
			ID string `json:"id"`
		}
		if err := json.Unmarshal(data, &env); err != nil || env.ID == "" {
			logf("malformed response: %s", string(data))
			continue
		}
		if !s.deliverResponse(env.ID, data) {
			logf("orphan response id=%s: %s", env.ID, string(data))
		}
	}
}

// HandleCommand forwards an HTTP-submitted command to the connected buddy.
func (s *State) HandleCommand(w http.ResponseWriter, r *http.Request) {
	var req struct {
		ID        string         `json:"id"`
		Action    string         `json:"action"`
		Params    map[string]any `json:"params"`
		TimeoutMs int            `json:"timeout_ms"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid json: " + err.Error()})
		return
	}
	if strings.TrimSpace(req.Action) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "missing action"})
		return
	}
	if req.Params == nil {
		req.Params = map[string]any{}
	}
	cmd := newCommand(req.Action, req.Params)
	if req.ID != "" {
		cmd.ID = req.ID
	}
	if req.TimeoutMs > 0 {
		cmd.TimeoutMs = req.TimeoutMs
	}

	timeout := 30 * time.Second
	if req.TimeoutMs > 0 {
		timeout = time.Duration(req.TimeoutMs)*time.Millisecond + 5*time.Second
	}
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()

	raw, err := s.Dispatch(ctx, cmd)
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": err.Error()})
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(raw)
}

// greet sends one `ping` after connect; runs in its own goroutine because Dispatch blocks on the WS read loop.
func (s *State) greet(buddyID string) {
	cmd := newCommand("ping", map[string]any{"from": "mock-device", "hello": true})
	ctx, cancel := context.WithTimeout(context.Background(), 7*time.Second)
	defer cancel()
	if _, err := s.Dispatch(ctx, cmd); err != nil {
		logf("hello ping failed for %s: %v", buddyID, err)
		return
	}
	logf("hello ping ok for %s", buddyID)
}

// Dispatch sends a command over the WS and waits for the matching response; ctx sets the timeout.
func (s *State) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	ws := s.currentWS()
	if ws == nil {
		return nil, errors.New("no buddy connected")
	}
	ch := s.registerPending(cmd.ID)
	defer s.cancelPending(cmd.ID)

	data, err := json.Marshal(cmd)
	if err != nil {
		return nil, fmt.Errorf("marshal: %w", err)
	}
	if err := ws.WriteMessage(websocket.TextMessage, data); err != nil {
		return nil, fmt.Errorf("write: %w", err)
	}

	select {
	case resp := <-ch:
		return resp, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(10 * time.Second):
		return nil, errors.New("timeout waiting for buddy response")
	}
}
