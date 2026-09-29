package openclaw

import (
	"testing"
)

// modern2026_5 mimics the production target runtime (2026.5.x), where the legacy streaming string is hard-rejected and the object form is required.
var modern2026_5 = RuntimeInfo{Year: 2026, Minor: 5, Patch: 27, Detected: true}

func TestApplySlackChannelConfig_HTTPMode(t *testing.T) {
	m := map[string]any{}
	applySlackChannelConfig(m, slackChannelConfig{
		BotToken:      "xoxb-bot",
		AppToken:      "xapp-app",
		UserID:        "U123",
		Mode:          "http",
		SigningSecret: "sign-secret",
		Runtime:       modern2026_5,
	})

	if m["mode"] != "http" {
		t.Fatalf("mode = %v, want http", m["mode"])
	}
	if m["signingSecret"] != "sign-secret" {
		t.Errorf("signingSecret = %v, want sign-secret", m["signingSecret"])
	}
	if m["webhookPath"] != "/slack/events" {
		t.Errorf("webhookPath = %v, want default /slack/events", m["webhookPath"])
	}
	// HTTP mode must not carry Socket-Mode-only keys.
	if _, ok := m["appToken"]; ok {
		t.Errorf("appToken must be deleted in http mode")
	}
	if _, ok := m["socketMode"]; ok {
		t.Errorf("socketMode must be deleted in http mode")
	}
	if m["groupPolicy"] != "open" {
		t.Errorf("groupPolicy = %v, want open", m["groupPolicy"])
	}
	if m["dmPolicy"] != "open" {
		t.Errorf("dmPolicy = %v, want open", m["dmPolicy"])
	}
	if got, ok := m["allowFrom"].([]string); !ok || len(got) != 1 || got[0] != "*" {
		t.Errorf("allowFrom = %v, want [*]", m["allowFrom"])
	}
	dm, ok := m["dm"].(map[string]any)
	if !ok || dm["enabled"] != true {
		t.Errorf("dm.enabled must be true, got %v", m["dm"])
	}
	st, ok := m["streaming"].(map[string]any)
	if !ok || st["mode"] != "partial" || st["nativeTransport"] != true {
		t.Errorf("streaming = %v, want object {mode:partial, nativeTransport:true}", m["streaming"])
	}
	if _, ok := m["requireMention"]; ok {
		t.Errorf("requireMention must be stripped")
	}
	if _, ok := m["nativeStreaming"]; ok {
		t.Errorf("nativeStreaming must be stripped")
	}
}

func TestApplySlackChannelConfig_SocketMode(t *testing.T) {
	m := map[string]any{}
	applySlackChannelConfig(m, slackChannelConfig{
		BotToken: "xoxb-bot",
		AppToken: "xapp-app",
		UserID:   "U123",
		Runtime:  modern2026_5,
	})

	if m["mode"] != "socket" {
		t.Fatalf("mode = %v, want socket", m["mode"])
	}
	if m["appToken"] != "xapp-app" {
		t.Errorf("appToken = %v, want xapp-app", m["appToken"])
	}
	// Socket mode must not carry HTTP-mode-only keys.
	if _, ok := m["signingSecret"]; ok {
		t.Errorf("signingSecret must be deleted in socket mode")
	}
	if _, ok := m["webhookPath"]; ok {
		t.Errorf("webhookPath must be deleted in socket mode")
	}
	sm, ok := m["socketMode"].(map[string]any)
	if !ok || sm["clientPingTimeout"] != 20000 || sm["serverPingTimeout"] != 30000 {
		t.Errorf("socketMode = %v, want clientPingTimeout=20000 serverPingTimeout=30000", m["socketMode"])
	}
}

func TestRuntimeVersionString(t *testing.T) {
	if got := runtimeVersionString(modern2026_5); got != "2026.5.27" {
		t.Errorf("runtimeVersionString(detected) = %q, want 2026.5.27", got)
	}
	if got := runtimeVersionString(RuntimeInfo{Year: 2026, Minor: 5, Patch: 27}); got != "" {
		t.Errorf("runtimeVersionString(undetected) = %q, want empty", got)
	}
}

func TestApplySlackChannelConfig_LegacyRuntimeStreamingString(t *testing.T) {
	legacy := RuntimeInfo{Year: 2026, Minor: 3, Patch: 13, Detected: true}
	m := map[string]any{}
	applySlackChannelConfig(m, slackChannelConfig{
		BotToken: "xoxb-bot",
		AppToken: "xapp-app",
		UserID:   "U123",
		Mode:     "socket",
		Runtime:  legacy,
	})

	if m["streaming"] != "partial" {
		t.Errorf("streaming = %v, want legacy string \"partial\"", m["streaming"])
	}
	if _, ok := m["socketMode"]; ok {
		t.Errorf("socketMode must not be set on pre-2026.4 runtime")
	}
}
