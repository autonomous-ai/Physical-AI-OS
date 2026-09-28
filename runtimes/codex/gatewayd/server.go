package gatewayd

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// closeUnauthorized is the WS close code for a bad/missing bearer token.
const closeUnauthorized = 4401

var errClientGone = errors.New("client gone")

var upgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	// Loopback-only service authenticated by bearer token; origin is moot.
	CheckOrigin: func(*http.Request) bool { return true },
}

// wsClient wraps a connection with a write mutex (gorilla allows only one
// concurrent writer) and a gone flag set on the first write error.
type wsClient struct {
	conn *websocket.Conn
	mu   sync.Mutex
	gone bool
}

func (c *wsClient) send(data []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.gone {
		return errClientGone
	}
	if err := c.conn.WriteMessage(websocket.TextMessage, data); err != nil {
		c.gone = true
		return err
	}
	return nil
}

func (c *wsClient) close(code int, reason string) {
	msg := websocket.FormatCloseMessage(code, reason)
	_ = c.conn.WriteControl(websocket.CloseMessage, msg, time.Now().Add(time.Second))
	_ = c.conn.Close()
}

// inboundFrame is the common envelope of client -> gatewayd frames.
type inboundFrame struct {
	Type    string          `json:"type"`
	RunID   string          `json:"run_id"`
	ID      json.RawMessage `json:"id"`
	Payload json.RawMessage `json:"payload"`
}

// turnPayload is the payload of a message.send frame.
type turnPayload struct {
	Source         string `json:"source,omitempty"`
	preload        string
	preloadChecked bool
	RequestID      string `json:"-"`
	RunID          string `json:"-"`
	Content        string `json:"content"`
	Attachments    []struct {
		Type string `json:"type"`
		URL  string `json:"url"`
	} `json:"attachments"`
}

// op kinds processed by the single worker (strict FIFO). session.new rides the
// same queue as turns so a mid-turn reset executes AFTER the in-flight/queued
// turns — clearing the thread id immediately would be undone by the in-flight
// turn's thread.started re-persisting the old id.
const (
	opTurn       = "turn"
	opSessionNew = "session.new"
)

// op is one unit of work for the worker queue.
type op struct {
	kind    string // opTurn | opSessionNew
	payload turnPayload
}

// handleWS upgrades the connection, enforces bearer auth (close 4401 on
// failure) and runs the read loop. A new client replaces the previous one.
func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	// The "/codex/ws/" mux pattern matches the whole subtree; only the two
	// exact paths are valid WS endpoints (mirrors bridge.py WS_PATHS).
	if r.URL.Path != "/codex/ws" && r.URL.Path != "/codex/ws/" {
		http.NotFound(w, r)
		return
	}
	authorized := r.Header.Get("Authorization") == "Bearer "+s.cfg.Token
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	client := &wsClient{conn: conn}
	if !authorized {
		log.Printf("%s unauthorized client rejected", logPrefix)
		client.close(closeUnauthorized, "unauthorized")
		return
	}
	conn.SetReadLimit(streamLimit)

	s.mu.Lock()
	old := s.client
	s.client = client
	threadID := s.threadID
	s.mu.Unlock()
	if old != nil {
		log.Printf("%s new client replaces previous connection", logPrefix)
		old.close(websocket.CloseNormalClosure, "replaced by new connection")
	}
	log.Printf("%s client connected", logPrefix)
	s.sendStatus("ready", threadID)

	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			break
		}
		s.handleFrame(data)
	}

	s.mu.Lock()
	if s.client == client {
		s.client = nil
	}
	s.mu.Unlock()
	_ = conn.Close()
	log.Printf("%s client disconnected", logPrefix)
}

// handleFrame dispatches one inbound frame. message.send is only enqueued
// here so pings keep flowing while a turn runs (worker serializes turns).
// Replies go through s.send: this connection IS the current client (single
// client invariant), so no per-frame connection plumbing is needed.
func (s *Server) handleFrame(data []byte) {
	var frame inboundFrame
	if err := json.Unmarshal(data, &frame); err != nil {
		log.Printf("%s dropping non-JSON frame", logPrefix)
		return
	}
	switch frame.Type {
	case "ping":
		pong := map[string]any{"type": "pong"}
		if len(frame.ID) > 0 {
			pong["id"] = frame.ID
		}
		s.sendJSON(pong)
	case "message.send":
		var payload turnPayload
		if len(frame.Payload) > 0 {
			if err := json.Unmarshal(frame.Payload, &payload); err != nil {
				log.Printf("%s bad message.send payload: %v", logPrefix, err)
				return
			}
		}
		_ = json.Unmarshal(frame.ID, &payload.RequestID)
		payload.RunID = frame.RunID
		s.mu.Lock()
		active := s.activeTurnID != "" || s.activeStarting
		s.mu.Unlock()
		if active {
			// Keep the original turn correlation. A steered message joins that
			// same reply rather than becoming a second independently completed turn.
			s.steerAppTurn(payload)
		} else {
			s.enqueue(op{kind: opTurn, payload: payload})
		}
	case "session.new":
		// Queued behind in-flight/queued turns (see op) — session_cleared is
		// sent when it actually executes.
		log.Printf("%s session.new queued — clears after in-flight/queued turns", logPrefix)
		s.enqueue(op{kind: opSessionNew})
	default:
		log.Printf("%s ignoring unknown frame type %q", logPrefix, frame.Type)
	}
}

// enqueue never blocks the read loop. A full queue rejects the queued request
// with its own correlation fields; it must not emit an uncorrelated bridge.error
// that would terminate the currently streaming turn. Legacy clients without
// correlation fields retain the bridge.status compatibility response.
func (s *Server) enqueue(o op) {
	select {
	case s.ops <- o:
	default:
		s.mu.Lock()
		threadID := s.threadID
		s.mu.Unlock()
		log.Printf("%s worker queue full — dropping %s frame", logPrefix, o.kind)
		if o.kind == opTurn && (o.payload.RequestID != "" || o.payload.RunID != "") {
			s.sendJSON(map[string]any{"type": "bridge.rejected", "error": "worker queue full", "request_id": o.payload.RequestID, "run_id": o.payload.RunID})
		} else {
			s.sendStatus("queue_full", threadID)
		}
	}
}

// send forwards raw bytes to the connected client, if any. Write errors mark
// the client gone but never fail the caller: a disconnect mid-turn must not
// kill the turn — the subprocess finishes so the session stays consistent.
func (s *Server) send(data []byte) {
	s.mu.Lock()
	client := s.client
	requestID, runID := s.activeRequestID, s.activeRunID
	s.mu.Unlock()
	data = correlateTurnFrame(data, requestID, runID)
	if client == nil {
		return
	}
	if err := client.send(data); err != nil {
		s.mu.Lock()
		if s.client == client {
			s.client = nil
		}
		s.mu.Unlock()
	}
}

func (s *Server) sendJSON(v any) {
	data, err := json.Marshal(v)
	if err != nil {
		log.Printf("%s marshal outbound frame failed: %v", logPrefix, err)
		return
	}
	s.send(data)
}

func (s *Server) sendStatus(state, threadID string) {
	s.sendJSON(map[string]any{
		"type":      "bridge.status",
		"state":     state,
		"thread_id": threadID,
	})
}

func (s *Server) sendError(msg string) {
	s.sendJSON(map[string]any{"type": "bridge.error", "error": msg})
}

// runCorrelatedTurn scopes metadata to the worker's whole turn, including
// resume fallback and terminal bridge errors. Control/status frames stay untagged.
func (s *Server) runCorrelatedTurn(ctx context.Context, payload turnPayload) {
	s.mu.Lock()
	s.activeRequestID, s.activeRunID = payload.RequestID, payload.RunID
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.activeRequestID, s.activeRunID = "", ""
		s.mu.Unlock()
	}()
	s.runTurn(ctx, payload)
}

// runCorrelatedAppTurn owns correlation for the lifetime of the App Server
// turn. Completion clears it in handleAppNotification, rather than on return.
func (s *Server) runCorrelatedAppTurn(payload turnPayload) {
	s.mu.Lock()
	if s.activeTurnID != "" {
		s.mu.Unlock()
		s.steerAppTurn(payload)
		return
	}
	if s.activeStarting {
		s.mu.Unlock()
		// A thread/start response is imminent. Preserve the message until it
		// yields a real turn id, then it will be steered rather than becoming a
		// competing fresh turn.
		go func() { time.Sleep(10 * time.Millisecond); s.enqueue(op{kind: opTurn, payload: payload}) }()
		return
	}
	s.activeRequestID, s.activeRunID = payload.RequestID, payload.RunID
	s.activeStarting = true
	s.mu.Unlock()
	s.startAppTurn(payload)
}

func correlateTurnFrame(data []byte, requestID, runID string) []byte {
	if requestID == "" && runID == "" {
		return data
	}
	var frame map[string]json.RawMessage
	if json.Unmarshal(data, &frame) != nil {
		return data
	}
	var kind string
	_ = json.Unmarshal(frame["type"], &kind)
	switch kind {
	case "thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated", "item.completed", "error", "bridge.error":
	default:
		return data
	}
	frame["request_id"], _ = json.Marshal(requestID)
	frame["run_id"], _ = json.Marshal(runID)
	encoded, err := json.Marshal(frame)
	if err != nil {
		return data
	}
	return encoded
}
