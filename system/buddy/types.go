// Package buddy is the device-side coordinator for the Autonomous Buddy macOS
// app: pairing, persisted pairing record, WebSocket gateway and command dispatch.
package buddy

import (
	"crypto/rand"
	"encoding/hex"
)

// Command matches the JSON shape the buddy expects on its WebSocket.
// Mirrors autonomous-buddy/mock-device/command.go.
type Command struct {
	ID        string         `json:"id"`
	Action    string         `json:"action"`
	Params    map[string]any `json:"params"`
	TimeoutMs int            `json:"timeout_ms,omitempty"`
	IssuedAt  string         `json:"issued_at,omitempty"`
	IssuedBy  string         `json:"issued_by,omitempty"`
}

// CommandResponse is the JSON shape the buddy returns over WebSocket.
type CommandResponse struct {
	ID         string         `json:"id"`
	OK         bool           `json:"ok"`
	Result     map[string]any `json:"result,omitempty"`
	Error      string         `json:"error,omitempty"`
	DurationMs int            `json:"duration_ms"`
}

// NewCommandID returns a fresh 16-hex-char ID for a command.
func NewCommandID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}
