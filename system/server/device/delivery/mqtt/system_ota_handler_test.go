package mqtthandler

import (
	"errors"
	"fmt"
	"testing"
	"time"

	"go.autonomous.ai/os/system/ota"
)

func TestSoftwareUpdateAckStartedIsTerminalSuccess(t *testing.T) {
	status, errMsg, data := softwareUpdateAck("agent", "hermes", nil)
	if status != "success" || errMsg != "" {
		t.Fatalf("status=%q err=%q", status, errMsg)
	}
	if data["target"] != "agent" || data["resolved_target"] != "hermes" || data["state"] != "started" {
		t.Fatalf("data = %v", data)
	}
}

func TestSoftwareUpdateAckRateLimited(t *testing.T) {
	err := &ota.RateLimitedError{Target: "hal", RetryAfter: 12 * time.Second}
	status, errMsg, data := softwareUpdateAck("hal", "hal", err)
	if status != "failure" || errMsg != "software-update hal rate-limited, retry in 13s" {
		t.Fatalf("status=%q err=%q", status, errMsg)
	}
	if data["target"] != "hal" || data["retry_after_seconds"] != 13 {
		t.Fatalf("data = %v", data)
	}
}

func TestSoftwareUpdateAckUnknownTarget(t *testing.T) {
	status, errMsg, data := softwareUpdateAck("nope", "nope", fmt.Errorf("%w: nope", ota.ErrUnknownTarget))
	if status != "failure" || errMsg != "unknown target: nope" {
		t.Fatalf("status=%q err=%q", status, errMsg)
	}
	if _, ok := data["retry_after_seconds"]; ok {
		t.Fatalf("no retry_after_seconds expected: %v", data)
	}
}

func TestSoftwareUpdateCompletion(t *testing.T) {
	upToDate := map[string]any{"hermes": map[string]any{"current": "0.2", "target": "0.2", "update_available": false}}
	status, _, data := softwareUpdateCompletion("agent", "hermes", nil, upToDate, nil)
	if status != "success" || data["state"] != "completed" || data["current"] != "0.2" ||
		data["target_version"] != "0.2" || data["update_available"] != false || data["target"] != "agent" {
		t.Fatalf("status=%q data=%v", status, data)
	}

	behind := map[string]any{"hal": map[string]any{"current": "1.0", "target": "1.1", "update_available": true}}
	status, errMsg, data := softwareUpdateCompletion("hal", "hal", nil, behind, nil)
	if status != "failure" || data["state"] != "failed" || data["update_available"] != true || errMsg == "" {
		t.Fatalf("status=%q err=%q data=%v", status, errMsg, data)
	}

	status, _, data = softwareUpdateCompletion("hal", "hal", errors.New("context deadline exceeded"), nil, nil)
	if status != "failure" || data["state"] != "failed" {
		t.Fatalf("timeout: status=%q data=%v", status, data)
	}

	status, _, _ = softwareUpdateCompletion("hal", "hal", nil, map[string]any{}, nil)
	if status != "failure" {
		t.Fatalf("missing entry must fail, got %q", status)
	}
}

func TestSoftwareUpdateAlertTitles(t *testing.T) {
	tests := []struct{ stage, requested, resolved, want string }{
		{"started", "agent", "openclaw", "⬆️ Software update agent (openclaw) — started"},
		{"success", "agent", "openclaw", "✅ Software update agent (openclaw) — done"},
		{"failure", "hal", "hal", "❌ Software update hal — failed"},
		{"rejected", "agent", "openclaw", "❌ Software update agent (openclaw) — rejected"},
		{"rejected", "nope", "", "❌ Software update nope — rejected"},
	}
	for _, tt := range tests {
		if got, _ := softwareUpdateAlert(tt.stage, tt.requested, tt.resolved, ""); got != tt.want {
			t.Errorf("softwareUpdateAlert(%q, %q, %q) title = %q, want %q", tt.stage, tt.requested, tt.resolved, got, tt.want)
		}
	}
}

func TestSoftwareUpdateAlertDetails(t *testing.T) {
	versions := map[string]any{"openclaw": map[string]any{"current": "2026.6.10", "target": "2026.9.3"}}
	if got := softwareUpdateStartDetail(versions, "openclaw"); got != "2026.6.10 → 2026.9.3" {
		t.Errorf("start detail = %q", got)
	}
	if got := softwareUpdateStartDetail(nil, "openclaw"); got != "" {
		t.Errorf("start detail without a version report = %q, want empty", got)
	}
	took := 4*time.Minute + 6*time.Second + 400*time.Millisecond
	if got := softwareUpdateDoneDetail("success", "", map[string]any{"current": "2026.9.3"}, took); got != "now 2026.9.3 · took 4m6s" {
		t.Errorf("success detail = %q", got)
	}
	if got := softwareUpdateDoneDetail("failure", "update did not finish: timeout", nil, took); got != "update did not finish: timeout · after 4m6s" {
		t.Errorf("failure detail = %q", got)
	}
}
