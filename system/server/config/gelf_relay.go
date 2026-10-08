package config

import (
	"strings"

	"go.autonomous.ai/os/system/lib/urlnorm"
)

// GELFRelayCredentials returns the cloud API base URL and device key that
// os-server's log relay (logger.EnableGELFRelay) ships through, or two empty
// strings when the device has no Autonomous credential to use.
func (c *Config) GELFRelayCredentials() (baseURL, apiKey string) {
	if d := c.AutonomousDefaults; d != nil {
		if base, key, ok := autonomousRelayTarget(d.BaseURL, d.APIKey); ok {
			return base, key
		}
	}
	if base, key, ok := autonomousRelayTarget(c.LLMBaseURL, c.LLMAPIKey); ok {
		return base, key
	}
	return "", ""
}

// AutonomousBackend returns the Autonomous cloud API base URL and device key
// for calls that carry account secrets (OAuth and connector refresh tokens):
// the backend channel, then the shipped defaults, then the active LLM pair,
// whichever first points at our own gateway. ok is false when none does, so a
// user-chosen LLM provider never receives those secrets.
func (c *Config) AutonomousBackend() (baseURL, apiKey string, ok bool) {
	pairs := [][2]string{{c.BackendBaseURL, c.BackendAPIKey}}
	if d := c.AutonomousDefaults; d != nil {
		pairs = append(pairs, [2]string{d.BaseURL, d.APIKey})
	}
	pairs = append(pairs, [2]string{c.LLMBaseURL, c.LLMAPIKey})
	for _, p := range pairs {
		if base, key, ok := autonomousRelayTarget(p[0], p[1]); ok {
			return base, key, true
		}
	}
	return "", "", false
}

// autonomousRelayTarget normalizes one base/key pair and reports whether it is
// complete and points at our own gateway.
func autonomousRelayTarget(rawBase, rawKey string) (base, key string, ok bool) {
	base = withAPIVersion(strings.TrimRight(strings.TrimSpace(rawBase), "/"))
	key = strings.TrimSpace(rawKey)
	return base, key, key != "" && urlnorm.IsAutonomousHost(base)
}

// withAPIVersion adds the version segment an older config.json may lack:
// config.json is normalized only when os-server saves it, so a file that was
// never re-saved can still hold the unversioned base.
func withAPIVersion(base string) string {
	if strings.HasSuffix(base, "/ai") && urlnorm.IsAutonomousHost(base) {
		return base + "/v1"
	}
	return base
}
