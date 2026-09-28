package http

import (
	"context"
	"time"

	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/intent/jev"
)

// matchVoiceIntent keeps configuration and credentials out of the intent
// catalogue. A nil resolver (older wiring/tests) retains local-only behavior.
func (h *SensingHandler) matchVoiceIntent(ctx context.Context, message string) *intent.Result {
	settings := h.config.JevIntentSettings()
	options := jev.Options{
		Enabled:  settings.Enabled,
		Endpoint: settings.Endpoint,
		APIKey:   settings.APIKey,
		Timeout:  time.Duration(settings.TimeoutMS) * time.Millisecond,
	}
	return intent.MatchWithFallback(ctx, message, h.intentResolver, options)
}
