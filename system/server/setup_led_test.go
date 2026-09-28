package server

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/config"
)

type setupLEDTransport func(*http.Request) (*http.Response, error)

func (f setupLEDTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestSetupLEDWaitsBeyondSlowBootAndRetriesRejectedPaint(t *testing.T) {
	var elapsed time.Duration
	var waits []time.Duration
	paints := 0
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = setupLEDTransport(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost || r.URL.Path != "/led/status" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		paints++
		if paints == 1 && elapsed != 0 {
			t.Fatal("first LED request must be immediate")
		}
		status, body := http.StatusOK, `{"status":"ok"}`
		if elapsed < 45*time.Second {
			status, body = http.StatusServiceUnavailable, `{"detail":"LED starting"}`
		} else if elapsed == 45*time.Second {
			body = `{"status":"error"}`
		}
		return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	retrySetupLED(context.Background(), func() bool { return false }, func(ctx context.Context) error {
		return hal.SetStatusContext(ctx, "setup")
	}, func(_ context.Context, delay time.Duration) bool {
		waits = append(waits, delay)
		elapsed += delay
		if elapsed > time.Minute {
			t.Fatal("worker did not stop after successful paint")
		}
		return true
	})
	if paints != 9 || elapsed != 55*time.Second {
		t.Fatalf("paints=%d elapsed=%v; want nine attempts ending at 55s", paints, elapsed)
	}
	for i, delay := range waits {
		want := min(time.Second*time.Duration(1<<i), 10*time.Second)
		if delay != want {
			t.Fatalf("wait %d = %v, want %v", i, delay, want)
		}
	}
}

func TestSetupLEDStopsWhenSetupCompletes(t *testing.T) {
	for _, stage := range []string{"before request", "during request", "during backoff"} {
		t.Run(stage, func(t *testing.T) {
			completed := stage == "before request"
			paints := 0
			retrySetupLED(context.Background(), func() bool { return completed }, func(context.Context) error {
				paints++
				if stage == "during request" {
					completed = true
				}
				return errors.New("LED unavailable")
			}, func(context.Context, time.Duration) bool {
				if completed {
					t.Fatal("must not wait after setup completes")
				}
				completed = true
				return true
			})
			if stage == "before request" && paints != 0 || paints > 1 {
				t.Fatalf("unexpected LED calls: %d", paints)
			}
		})
	}
}

func TestSetupLEDShutdownCancelsRequestAndBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	retrySetupLED(ctx, func() bool { return false }, func(ctx context.Context) error {
		cancel()
		return ctx.Err()
	}, func(context.Context, time.Duration) bool {
		t.Fatal("must not wait after shutdown")
		return false
	})
	if waitSetupLED(ctx, time.Hour) {
		t.Fatal("cancelled wait must exit without another attempt")
	}
	retrySetupLED(ctx, func() bool { return false }, func(context.Context) error {
		t.Fatal("must not paint after shutdown")
		return nil
	}, waitSetupLED)
}

func TestSetupLEDReadyImmediatelyWithoutHealthyHAL(t *testing.T) {
	paints := 0
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = setupLEDTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path == "/health" {
			t.Fatal("setup LED must not wait for full HAL health")
		}
		if r.Method != http.MethodPost || r.URL.Path != "/led/status" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		paints++
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`))}, nil
	})
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	s := &Server{config: &config.Config{DeviceType: "lamp"}}
	s.waitAndPaintSetupReady(ctx)
	if paints != 1 {
		t.Fatalf("paints=%d, want one", paints)
	}
}
