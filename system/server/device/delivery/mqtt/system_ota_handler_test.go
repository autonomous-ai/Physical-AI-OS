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
	// The backend's listen mode waits for a terminal status; "success" with
	// state "started" is what ends it.
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
