package gatewayd

import (
	"encoding/json"
	"errors"
	"fmt"
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
	Payload json.RawMessage `json:"payload"`
}

// turnPayload is the payload of a message.send frame.
type turnPayload struct {
	Source         string `json:"source,omitempty"`
	preload        string
	preloadChecked bool
	Content        string `json:"content"`
	Attachments    []struct {
		Type string `json:"type"`
		URL  string `json:"url"`
	} `json:"attachments"`
}

// handleWS upgrades the connection, enforces bearer auth (close 4401 on
// failure) and runs the read loop. A new client replaces the previous one.
func (s *Server) handleWS(w http.ResponseWriter, r *http.Request) {
	// The "/claude/ws/" mux pattern matches the whole subtree; only the two
	// exact paths are valid WS endpoints.
	if r.URL.Path != "/claude/ws" && r.URL.Path != "/claude/ws/" {
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
	s.mu.Unlock()
	if old != nil {
		log.Printf("%s new client replaces previous connection", logPrefix)
		old.close(websocket.CloseNormalClosure, "replaced by new connection")
	}
	log.Printf("%s client connected", logPrefix)
	s.sendStatus(nil)

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

// handleFrame dispatches one inbound frame. message.send is written to the
// child stdin immediately (claude serializes queued turns itself); frames that
// arrive while the child is down are queued and flushed on respawn. Replies go
// through s.send: this connection IS the current client (single-client
// invariant), so no per-frame connection plumbing is needed.
func (s *Server) handleFrame(data []byte) {
	var frame inboundFrame
	if err := json.Unmarshal(data, &frame); err != nil {
		return // unparseable frames are ignored (bridge.py behavior)
	}
	switch frame.Type {
	case "ping":
		// bridge.py answers a bare {"type":"pong"} without echoing the id.
		s.sendJSON(map[string]any{"type": "pong"})
	case "message.send":
		var payload turnPayload
		if len(frame.Payload) > 0 {
			if err := json.Unmarshal(frame.Payload, &payload); err != nil {
				log.Printf("%s bad message.send payload: %v", logPrefix, err)
				return
			}
		}
		s.sendUserMessage(payload)
	case "session.new":
		s.newSession()
	default:
		log.Printf("%s ignoring unknown frame type %q", logPrefix, frame.Type)
	}
}

// send forwards raw bytes to the connected client, if any. Write errors mark
// the client gone but never fail the caller: a disconnect must not disturb the
// child — the turn finishes so the session stays consistent.
func (s *Server) send(data []byte) {
	s.mu.Lock()
	client := s.client
	s.mu.Unlock()
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

// sendStatus emits a bridge.status frame:
// {"type":"bridge.status","payload":{"claude_running":<bool>,"session_id":<string|null>,..extra}}.
// The payload field names match what runtimes/claudecode/translator.go expects.
func (s *Server) sendStatus(extra map[string]any) {
	s.mu.Lock()
	running := s.child != nil
	sid := s.sessionID
	s.mu.Unlock()
	payload := map[string]any{
		"claude_running": running,
		"session_id":     nil, // JSON null when no session yet (bridge.py compat)
	}
	if sid != "" {
		payload["session_id"] = sid
	}
	for k, v := range extra {
		payload[k] = v
	}
	s.sendJSON(map[string]any{"type": "bridge.status", "payload": payload})
}

// sendBridgeError emits {"type":"bridge.error","payload":{"message":..}} — the
// translator maps it to lifecycle.error, closing the client-side turn.
func (s *Server) sendBridgeError(format string, args ...any) {
	s.sendJSON(map[string]any{
		"type":    "bridge.error",
		"payload": map[string]any{"message": fmt.Sprintf(format, args...)},
	})
}
