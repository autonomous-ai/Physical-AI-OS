package hermes

import (
	"log/slog"
	"strings"
)

// ApplyExternalEndpoint retargets Hermes at a server on another machine — the Intern is a voice/chat frontend for that server instead of the local install.
func ApplyExternalEndpoint(url, token string) {
	if u := strings.TrimSpace(url); u != "" {
		BaseURL = u
	}
	if t := strings.TrimSpace(token); t != "" {
		APIKey = t
	}
	slog.Info("hermes remote endpoint applied",
		"component", "hermes",
		"base_url", BaseURL,
		"api_key_set", APIKey != "",
	)
}
