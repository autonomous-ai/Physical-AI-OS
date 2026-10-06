package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func writeMessagesConfig(t *testing.T, dir string, data map[string]any) string {
	t.Helper()
	path := filepath.Join(dir, "openclaw.json")
	b, err := json.Marshal(data)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func readMessagesConfig(t *testing.T, path string) map[string]any {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

// Older setups wrote responsePrefix "auto", which prints "[main]" before every reply.
func TestEnsureMessagesConfigDropsAutoReplyPrefix(t *testing.T) {
	dir := t.TempDir()
	path := writeMessagesConfig(t, dir, map[string]any{
		"messages": map[string]any{"responsePrefix": "auto", "queue": map[string]any{"mode": "steer"}},
		"channels": map[string]any{
			"telegram": map[string]any{"responsePrefix": "auto", "botToken": "x"},
			"discord": map[string]any{
				"responsePrefix": "[bot]",
				"accounts":       map[string]any{"a": map[string]any{"responsePrefix": "auto"}},
			},
		},
	})
	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: dir}}

	changed, err := s.ensureMessagesQueueConfig()
	if err != nil || !changed {
		t.Fatalf("changed=%v err=%v, want a rewrite", changed, err)
	}
	got := readMessagesConfig(t, path)
	want := map[string]any{
		"messages": map[string]any{"queue": map[string]any{"mode": "steer"}},
		"channels": map[string]any{
			"telegram": map[string]any{"botToken": "x"},
			"discord": map[string]any{
				"responsePrefix": "[bot]", // a custom prefix is the owner's choice
				"accounts":       map[string]any{"a": map[string]any{}},
			},
		},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("openclaw.json = %v\nwant %v", got, want)
	}

	if changed, err := s.ensureMessagesQueueConfig(); err != nil || changed {
		t.Fatalf("second pass changed=%v err=%v, want no-op", changed, err)
	}
}

func TestEnsureMessagesConfigStillPinsSteer(t *testing.T) {
	dir := t.TempDir()
	path := writeMessagesConfig(t, dir, map[string]any{"messages": map[string]any{"queue": map[string]any{"mode": "collect"}}})
	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: dir}}

	if changed, err := s.ensureMessagesQueueConfig(); err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	queue := readMessagesConfig(t, path)["messages"].(map[string]any)["queue"].(map[string]any)
	if queue["mode"] != "steer" {
		t.Fatalf("queue.mode = %v", queue["mode"])
	}
}

// An empty heartbeat reply is a failure that OpenClaw posts to the owner's chat.
func TestHeartbeatEndsWithNoReplyNotSilence(t *testing.T) {
	if strings.Contains(heartbeatMDBlock, "skip silently") {
		t.Fatal(`"skip silently" invites an empty reply`)
	}
	if !strings.Contains(heartbeatMDBlock, "reply with exactly `NO_REPLY`") {
		t.Fatal("the heartbeat must end with NO_REPLY")
	}
	if !strings.HasSuffix(strings.TrimSpace(heartbeatMDBlock), "---") {
		t.Fatal("the OS block must still end at its --- separator")
	}
}
