package claudecode

// Wire constants for the Claude Code backend.
const (
	// WSURL is the local bridge WebSocket endpoint (served by the gatewayd —
	// runtimes/claudecode/gatewayd, default port 18791).
	WSURL = "ws://127.0.0.1:18791/claude/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	Token = "autonomous_claudecode_token"

	// Conversation is a label only — Claude Code owns its session ids; the real
	// session UUID is captured from the stream-json `system:init` event.
	Conversation = "device-main"

	// claudecodeHome is the backend's device-local state dir: .env
	// (ANTHROPIC_* + channel launch flags, presync-owned), session.json, and the
	// workspace/ Claude Code runs in.
	claudecodeHome = "/root/.claudecode"

	// EnvFile is the presync-owned launch env (ANTHROPIC_* creds + channel
	// flags).
	EnvFile = claudecodeHome + "/.env"
)
