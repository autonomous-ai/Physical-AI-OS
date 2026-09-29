package hermes

// BaseURL, APIKey are mutable so the "remote" runtime can target another Hermes server (ApplyExternalEndpoint).
var (
	BaseURL      = "http://127.0.0.1:8642"
	APIKey       = "hermes-local-api-key"
	Conversation = "device-main"
	Model        = "hermes-agent"
)
