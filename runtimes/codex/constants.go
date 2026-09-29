package codex

import "go.autonomous.ai/os/system/lib/syspath"

// Wire constants for the Codex backend.
const Conversation = "device-main"

// Resolved once at process start from the same env vars the gatewayd and
// presync.sh read (syspath).
var (
	// WSURL is the local bridge WebSocket endpoint (CODEX_PORT).
	WSURL = "ws://127.0.0.1:" + syspath.CodexPort() + "/codex/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	Token = syspath.CodexWSToken()

	// codexHome is the backend's device-local state dir: CODEX_HOME for the
	// CLI (config.toml, auth, sessions/) plus the .env, session.json and the
	// workspace/ Codex runs in.
	codexHome = syspath.CodexHome()
)
