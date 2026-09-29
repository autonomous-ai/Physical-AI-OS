package opencode

// Wire constants for the OpenCode backend.
const (
	// WSURL is the local bridge WebSocket endpoint (served by the gatewayd —
	// runtimes/opencode/gatewayd, default port 18793).
	WSURL = "ws://127.0.0.1:18793/opencode/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	Token = "autonomous_opencode_token"

	// Conversation is a label only — opencode owns its session ids; the real
	// session id is captured from the sessionID field on every run JSONL line.
	Conversation = "device-main"

	// opencodeHome is the backend's device-local state dir: the presync-owned
	// .env, session.json, attachments/ and the workspace/ opencode runs in.
	opencodeHome = "/root/.opencode"
)
