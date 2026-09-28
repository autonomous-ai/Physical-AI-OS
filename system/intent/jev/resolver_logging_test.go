package jev

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestResolverSkippedReasonsExcludeInputAndCredentials(t *testing.T) {
	for _, reason := range []string{"disabled", "missing_config", "invalid_input", "no_candidates", "cooldown", "busy", "cancelled"} {
		t.Run(reason, func(t *testing.T) {
			var logs bytes.Buffer
			previous := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
			defer slog.SetDefault(previous)
			r := NewResolver()
			o := Options{Enabled: true, Endpoint: "https://proxy.test", APIKey: "secret-key"}
			input := "private-utterance"
			candidates := Candidates()
			ctx := context.Background()
			switch reason {
			case "disabled":
				o.Enabled = false
			case "missing_config":
				o.Endpoint = ""
			case "invalid_input":
				input = ""
			case "no_candidates":
				candidates = nil
			case "cooldown":
				r.retryAfter.Store(time.Now().Add(time.Minute).UnixNano())
			case "busy":
				r.busy.Store(true)
			case "cancelled":
				var cancel context.CancelFunc
				ctx, cancel = context.WithCancel(ctx)
				cancel()
			}
			if result := r.Resolve(ctx, input, candidates, o); result.Intent != "" {
				t.Fatal(result)
			}
			out := logs.String()
			if !strings.Contains(out, `"reason":"`+reason+`"`) || strings.Contains(out, "private-utterance") || strings.Contains(out, "secret-key") {
				t.Fatal(out)
			}
		})
	}
}
