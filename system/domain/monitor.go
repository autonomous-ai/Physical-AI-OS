package domain

// MonitorEvent represents a single observable event in the agent workflow.
type MonitorEvent struct {
	ID      string `json:"id"`
	Time    string `json:"time"`
	Type    string `json:"type"` // "lifecycle", "chat_response", "sensing_input", "chat_send", "tts"
	Summary string `json:"summary"`
	Detail  any    `json:"detail,omitempty"`
	RunID   string `json:"runId,omitempty"`
	Phase   string `json:"phase,omitempty"`
	State   string `json:"state,omitempty"` // "partial", "final", etc.
	Error   string `json:"error,omitempty"`
}
