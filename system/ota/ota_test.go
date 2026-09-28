package ota

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// fakeBootstrap points bootstrapBaseURL at h for the test and resets the shared
// rate limiter so tests stay independent.
func fakeBootstrap(t *testing.T, h http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(h)
	prev := bootstrapBaseURL
	bootstrapBaseURL = srv.URL
	lastFireMu.Lock()
	lastFire = map[string]time.Time{}
	lastFireMu.Unlock()
	t.Cleanup(func() {
		srv.Close()
		bootstrapBaseURL = prev
	})
}

func hermesCfg() *config.Config { return &config.Config{AgentRuntime: "hermes"} }

func TestTriggerUpdateResolvesAgentAndRateLimits(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.Method+" "+r.URL.Path)
		mu.Unlock()
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})

	resolved, err := TriggerUpdate(context.Background(), hermesCfg(), "agent")
	if err != nil || resolved != "hermes" {
		t.Fatalf("first trigger: resolved=%q err=%v", resolved, err)
	}
	if len(paths) != 1 || paths[0] != "POST /force-update/hermes" {
		t.Fatalf("bootstrap calls = %v", paths)
	}

	// "agent" and "hermes" share one limiter slot (keyed on the resolved target).
	_, err = TriggerUpdate(context.Background(), hermesCfg(), "hermes")
	var rl *RateLimitedError
	if !errors.As(err, &rl) {
		t.Fatalf("second trigger: want *RateLimitedError, got %v", err)
	}
	if rl.RetryAfterSeconds() < 1 || rl.RetryAfterSeconds() > int(MinTriggerInterval.Seconds())+1 {
		t.Fatalf("retry after = %d", rl.RetryAfterSeconds())
	}
	if !strings.HasPrefix(err.Error(), "software-update hermes rate-limited, retry in ") {
		t.Fatalf("message = %q", err.Error())
	}
	if len(paths) != 1 {
		t.Fatalf("rate-limited trigger must not reach bootstrap: %v", paths)
	}

	// A different target is not limited.
	if _, err := TriggerUpdate(context.Background(), hermesCfg(), "hal"); err != nil {
		t.Fatalf("hal trigger: %v", err)
	}
}

func TestTriggerUpdateUnknownTarget(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("unknown target must not reach bootstrap: %s", r.URL.Path)
	})
	_, err := TriggerUpdate(context.Background(), hermesCfg(), "openclaw-nope")
	if !errors.Is(err, ErrUnknownTarget) || err.Error() != "unknown target: openclaw-nope" {
		t.Fatalf("err = %v", err)
	}
}

func TestTriggerUpdateBootstrapRefused(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":"unknown target: web"}` + "\n"))
	})
	_, err := TriggerUpdate(context.Background(), hermesCfg(), "web")
	var refused *BootstrapRefusedError
	if !errors.As(err, &refused) {
		t.Fatalf("want *BootstrapRefusedError, got %v", err)
	}
	want := `bootstrap refused web: 400 Bad Request {"error":"unknown target: web"}`
	if err.Error() != want {
		t.Fatalf("message = %q, want %q", err.Error(), want)
	}
}

func TestBootstrapUnreachable(t *testing.T) {
	fakeBootstrap(t, func(http.ResponseWriter, *http.Request) {})
	bootstrapBaseURL = "http://127.0.0.1:1" // nothing listens there
	if _, err := TriggerUpdate(context.Background(), hermesCfg(), "web"); !errors.Is(err, ErrBootstrapUnreachable) {
		t.Fatalf("trigger: %v", err)
	}
	if _, err := Versions(context.Background(), hermesCfg()); !errors.Is(err, ErrBootstrapUnreachable) {
		t.Fatalf("versions: %v", err)
	}
	if _, err := Updating(context.Background(), hermesCfg()); !errors.Is(err, ErrBootstrapUnreachable) {
		t.Fatalf("updating: %v", err)
	}
}

func TestVersionsAddsAgentAlias(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/versions" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{
			"hal":{"current":"1.0.0","target":"1.0.0","min_version":"1.0.0","update_available":false,"held_by_floor":false},
			"hermes":{"current":"0.1","target":"0.2","min_version":"0.1","update_available":true,"held_by_floor":true}}`))
	})
	v, err := Versions(context.Background(), hermesCfg())
	if err != nil {
		t.Fatal(err)
	}
	agent, ok := Component(v, "agent")
	if !ok || agent.Current != "0.1" || agent.Target != "0.2" || !agent.UpdateAvailable || !agent.HeldByFloor {
		t.Fatalf("agent alias = %+v ok=%v", agent, ok)
	}
	if _, ok := Component(v, "codex"); ok {
		t.Fatal("codex must be absent")
	}
}

func TestVersionsNoAliasForMissingRuntime(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"hal":{"current":"1"}}`))
	})
	v, err := Versions(context.Background(), &config.Config{AgentRuntime: "codex"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := v["agent"]; ok {
		t.Fatal("no agent alias expected when the runtime has no entry")
	}
}

func TestVersionsBadStatus(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	_, err := Versions(context.Background(), hermesCfg())
	if err == nil || err.Error() != "bootstrap versions: 500 Internal Server Error" {
		t.Fatalf("err = %v", err)
	}
}

func TestUpdatingAddsAgentAlias(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"updating":["hal","hermes"]}`))
	})
	got, err := Updating(context.Background(), hermesCfg())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(got, ",") != "hal,hermes,agent" {
		t.Fatalf("updating = %v", got)
	}
}

func TestUpdatingDecodeError(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`not json`))
	})
	_, err := Updating(context.Background(), hermesCfg())
	if err == nil || !strings.HasPrefix(err.Error(), "decode updating: ") {
		t.Fatalf("err = %v", err)
	}
}

func TestWaitUntilDoneAfterTargetDisappears(t *testing.T) {
	var calls atomic.Int32
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) <= 2 {
			_, _ = w.Write([]byte(`{"updating":["hal"]}`))
			return
		}
		_, _ = w.Write([]byte(`{"updating":[]}`))
	})
	opts := WaitOptions{Poll: 5 * time.Millisecond, AppearGrace: time.Hour}
	if err := WaitUntilDone(context.Background(), hermesCfg(), "hal", opts); err != nil {
		t.Fatal(err)
	}
	if calls.Load() < 3 {
		t.Fatalf("returned before the target disappeared (calls=%d)", calls.Load())
	}
}

func TestWaitUntilDoneNeverSeen(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"updating":[]}`))
	})
	opts := WaitOptions{Poll: 5 * time.Millisecond, AppearGrace: 20 * time.Millisecond}
	if err := WaitUntilDone(context.Background(), hermesCfg(), "hal", opts); err != nil {
		t.Fatal(err)
	}
}

func TestWaitUntilDoneTimesOutWhileUpdating(t *testing.T) {
	fakeBootstrap(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"updating":["hal"]}`))
	})
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	opts := WaitOptions{Poll: 5 * time.Millisecond, AppearGrace: time.Millisecond}
	if err := WaitUntilDone(ctx, hermesCfg(), "hal", opts); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("err = %v", err)
	}
}
