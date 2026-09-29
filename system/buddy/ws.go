package buddy

import (
	"encoding/json"
	"log/slog"

	"github.com/gorilla/websocket"
)

// RunReadLoop routes buddy responses (by `id`) to Dispatch callers until the
// connection closes. Run it in a goroutine after RegisterConnection.
func (s *Service) RunReadLoop(conn *websocket.Conn, buddyID string, agentHandlers ...func(AgentEvent)) {
	defer func() {
		s.clearConnection(conn)
		_ = conn.Close()
		slog.Info("buddy disconnected", "component", "buddy", "id", buddyID)
	}()

	conn.SetPongHandler(func(string) error { return nil })
	slog.Info("buddy connected", "component", "buddy", "id", buddyID)

	for {
		_, data, err := conn.ReadMessage()
		if err != nil {
			if !websocket.IsCloseError(err, websocket.CloseNormalClosure, websocket.CloseGoingAway) {
				slog.Warn("WS read error", "component", "buddy", "error", err)
			}
			return
		}
		var kind struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(data, &kind) == nil && kind.Type == "agent_event" {
			event, accepted := s.acceptAgentEvent(conn, buddyID, data)
			if accepted && len(agentHandlers) > 0 && agentHandlers[0] != nil {
				agentHandlers[0](event)
			}
			continue
		}
		var env struct {
			ID       string `json:"id"`
			OK       *bool  `json:"ok"`
			Error    string `json:"error"`
			Duration int    `json:"duration_ms"`
		}
		if err := json.Unmarshal(data, &env); err != nil || env.ID == "" {
			slog.Warn("malformed response from buddy", "component", "buddy")
			continue
		}
		ok := env.OK != nil && *env.OK
		slog.Info("buddy WS ← response", "component", "buddy", "id", env.ID, "ok", ok, "error", env.Error, "duration_ms", env.Duration, "bytes", len(data))
		if !s.registry.DeliverResponse(conn, env.ID, data) {
			slog.Warn("orphan response (no pending caller)", "component", "buddy", "id", env.ID)
		}
	}
}
