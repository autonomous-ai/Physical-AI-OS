package http

import "testing"

func TestCameraSnapshotURL(t *testing.T) {
	tests := []struct {
		name     string
		toolArgs string
		result   string
		want     string
	}{
		{
			name:     "saved camera snapshot",
			toolArgs: `{"command":"curl -s 'http://127.0.0.1:5001/camera/snapshot?save=true'"}`,
			result:   `{"path":"/root/.openclaw/media/hal-snapshots/snap_1710000000000.jpg"}`,
			want:     "/api/sensing/agent-snapshot/openclaw/media-hal-snapshots/snap_1710000000000.jpg",
		},
		{
			name:     "agent workspace JPEG",
			toolArgs: `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`,
			result:   `{"path":"/root/.openclaw/workspace/cam_face3.jpg"}`,
			want:     "/api/sensing/agent-snapshot/openclaw/workspace/cam_face3.jpg",
		},
		{
			name:     "non camera result is not exposed",
			toolArgs: `{"command":"curl -s http://127.0.0.1:5001/servo/play"}`,
			result:   `{"path":"/root/.openclaw/media/hal-snapshots/snap_1710000000000.jpg"}`,
			want:     "",
		},
		{
			name:     "untrusted filename is not exposed",
			toolArgs: `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`,
			result:   `{"path":"/etc/passwd"}`,
			want:     "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := cameraSnapshotURL(tt.toolArgs, tt.result); got != tt.want {
				t.Fatalf("cameraSnapshotURL() = %q, want %q", got, tt.want)
			}
		})
	}
}

// Codex-shaped pair: args arrive on "start", the result on "end"; remember, then resolve on the same event.
func TestSnapshotURLForToolCallCarriesArgsFromStart(t *testing.T) {
	h := &AgentHandler{}
	args := `/bin/bash -lc "curl -s http://127.0.0.1:5001/camera/snapshot?save=true"`
	result := `{"path": "/root/.codex/media/hal-snapshots/snap_1788498402794.jpg"}`

	h.rememberToolArgs("item_1", args)
	if got := h.snapshotURLForToolCall("item_1", args, ""); got != "" {
		t.Fatalf("start event must not yield a URL, got %q", got)
	}

	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1788498402794.jpg"
	if got := h.snapshotURLForToolCall("item_1", "", result); got != want {
		t.Fatalf("end event: got %q, want %q", got, want)
	}
	// Consumed: a replayed end event must not resurrect it.
	if got := h.snapshotURLForToolCall("item_1", "", result); got != "" {
		t.Fatalf("remembered args must be consumed once, got %q", got)
	}
}

// The detector must recognize both os-server's /api/vision/look and HAL's snapshot endpoint.
func TestSnapshotURLForToolCallAcceptsLookEndpoint(t *testing.T) {
	h := &AgentHandler{}
	args := `/bin/bash -lc "curl -sX POST http://127.0.0.1:5000/api/vision/look -d '{}'"`
	result := `{"status":1,"data":{"path":"/root/.codex/media/hal-snapshots/snap_1788500200004.jpg","description":"an office"}}`

	h.rememberToolArgs("item_2", args)
	h.snapshotURLForToolCall("item_2", args, "")

	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1788500200004.jpg"
	if got := h.snapshotURLForToolCall("item_2", "", result); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

// A search sweep's persisted frame must surface a thumbnail like a snapshot (#342 defect I).
func TestCameraSnapshotURLAcceptsASearchResult(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{\"target\":\"keyboard\"}'"}`
	result := `{"found":true,"kind":"keyboard","image_path":"/root/.codex/media/hal-snapshots/snap_1757500000000.jpg"}`
	want := "/api/sensing/agent-snapshot/codex/media-hal-snapshots/snap_1757500000000.jpg"
	if got := cameraSnapshotURL(args, result); got != want {
		t.Fatalf("search snapshot not surfaced: got %q, want %q", got, want)
	}
}

// Every runtime in hal/config.py _AGENT_CONFIG_DIRS, including opencode, must be allow-listed.
func TestCameraSnapshotURLCoversEveryRuntimeHALWritesTo(t *testing.T) {
	for _, runtime := range []string{
		"openclaw", "hermes", "picoclaw", "codex", "claudecode", "opencode",
	} {
		args := `curl -s http://127.0.0.1:5001/camera/snapshot?save=true`
		result := `{"path":"/root/.` + runtime + `/media/hal-snapshots/snap_42.jpg"}`
		want := "/api/sensing/agent-snapshot/" + runtime + "/media-hal-snapshots/snap_42.jpg"
		if got := cameraSnapshotURL(args, result); got != want {
			t.Errorf("%s: got %q, want %q", runtime, got, want)
		}
	}
}

// Naming the search endpoint in untrusted tool output must not make an arbitrary path servable.
func TestCameraSnapshotURLStillRejectsAnUnapprovedSearchPath(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{}'"}`
	for _, result := range []string{
		`{"image_path":"/etc/shadow.jpg"}`,
		`{"image_path":"/root/.codex/../../etc/secret.jpg"}`,
		`{"image_path":"/root/.evilruntime/media/hal-snapshots/snap_1.jpg"}`,
	} {
		if got := cameraSnapshotURL(args, result); got != "" {
			t.Errorf("unapproved path surfaced for %s: %q", result, got)
		}
	}
}

// A sweep that found nothing must not yield a URL.
func TestCameraSnapshotURLIgnoresASearchThatFoundNothing(t *testing.T) {
	args := `{"command":"curl -sX POST http://127.0.0.1:5001/servo/search -d '{}'"}`
	result := `{"found":false,"image_path":null,"message":"no keyboard found after 18 look(s)"}`
	if got := cameraSnapshotURL(args, result); got != "" {
		t.Fatalf("a miss produced a thumbnail: %q", got)
	}
}

// A backgrounded sweep's poll result carrying image_path under an approved path is trusted on the path alone.
func TestCameraSnapshotURLAcceptsASearchResultDeliveredByAPoll(t *testing.T) {
	args := `{"action":"poll","sessionId":"brisk-bison","timeout":15000}`
	result := `{"status":"ok","found":true,"kind":"doll","image_path":"/root/.openclaw/media/hal-snapshots/snap_1789363665350.jpg","looks_visited":2}`
	want := "/api/sensing/agent-snapshot/openclaw/media-hal-snapshots/snap_1789363665350.jpg"
	if got := cameraSnapshotURL(args, result); got != want {
		t.Errorf("polled search result not surfaced: got %q, want %q", got, want)
	}
}

// Only image_path is relaxed: a generic `path` still needs the endpoint, and unapproved dirs are refused.
func TestCameraSnapshotURLPollRelaxationIsScopedToImagePath(t *testing.T) {
	poll := `{"action":"poll","sessionId":"x"}`
	if got := cameraSnapshotURL(poll, `{"path":"/root/.openclaw/media/hal-snapshots/snap_1.jpg"}`); got != "" {
		t.Errorf("a generic path on a poll was surfaced: %q", got)
	}
	if got := cameraSnapshotURL(poll, `{"image_path":"/etc/shadow.jpg"}`); got != "" {
		t.Errorf("an unapproved image_path was surfaced: %q", got)
	}
}
