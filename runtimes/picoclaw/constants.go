package picoclaw

// Wire constants for the PicoClaw backend.
const (
	// WSURL is the PicoClaw WebSocket endpoint.
	WSURL = "ws://127.0.0.1:18790/pico/ws/"

	// Token is the bearer token sent in the Authorization header on connect.
	Token = "darren_pico_token"

	// Conversation is the default session name everything flows into until the
	// server assigns a session_id on its first frame.
	Conversation = "device-main"
)
