package hal

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestTurnSpeechPolicyRetainsOriginAcrossSegmentsAndRetry(t *testing.T) {
	var registry turnSpeechPolicyRegistry
	now := time.Now()
	registry.register("ambient", true, now)
	registry.register("ambient", false, now.Add(time.Second))
	registry.register("voice", false, now)
	registry.register("", true, now)
	for range 3 {
		if !registry.passive("ambient", now.Add(time.Minute)) {
			t.Fatal("streamed segment lost original passive policy")
		}
	}
	for _, id := range []string{"voice", "unknown", ""} {
		if registry.passive(id, now) {
			t.Fatalf("legacy/direct run %q became passive", id)
		}
	}
	if registry.passive("ambient", now.Add(turnSpeechPolicyTTL)) {
		t.Fatal("expired origin retained")
	}
}

func TestTurnSpeechPolicyBoundsAndExpiry(t *testing.T) {
	var registry turnSpeechPolicyRegistry
	now := time.Now()
	for i := range turnSpeechPolicyLimit + 1 {
		registry.register(fmt.Sprint(i), true, now.Add(time.Duration(i)*time.Millisecond))
	}
	if len(registry.entries) != turnSpeechPolicyLimit || registry.passive("0", now) {
		t.Fatal("registry failed to evict oldest entry at capacity")
	}
	registry.register("fresh", true, now.Add(2*turnSpeechPolicyTTL))
	if len(registry.entries) != 1 {
		t.Fatal("registration did not prune expired entries")
	}
}

func TestTurnSpeechPolicyConcurrentSegments(t *testing.T) {
	var registry turnSpeechPolicyRegistry
	now := time.Now()
	var wg sync.WaitGroup
	for i := range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			id := fmt.Sprint(i)
			registry.register(id, true, now)
			for range 20 {
				if !registry.passive(id, now) {
					t.Errorf("concurrent lookup lost %s", id)
				}
			}
		}()
	}
	wg.Wait()
}

func TestSpeakQueueReplyForTurnCarriesPassiveOriginOnEverySegment(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	runID := t.Name()
	RegisterTurnSpeechPolicy(runID, true)
	defer func() {
		turnSpeechPolicies.mu.Lock()
		delete(turnSpeechPolicies.entries, runID)
		turnSpeechPolicies.mu.Unlock()
	}()
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		wantPassive := payload["turn_id"] == runID
		if r.URL.Path != "/voice/speak-queue" || payload["passive_sensing"] != wantPassive || payload["realtime_feedback"] != true || payload["turn_seq"] != float64(7) {
			t.Fatalf("unexpected queue payload: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`))}, nil
	})}
	for _, id := range []string{runID, runID, "unknown-run"} {
		if err := SpeakQueueReplyForTurn("segment", id, 7); err != nil {
			t.Fatal(err)
		}
	}
}

func TestPassiveSpeechCaptureSuppressionIsNotSuccessfulPlayback(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	runID := t.Name()
	RegisterTurnSpeechPolicy(runID, true)
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["passive_sensing"] != true || payload["turn_id"] != runID {
			t.Fatalf("lost passive origin: %v", payload)
		}
		if r.URL.Path == "/voice/speak" && payload["cached"] != true {
			t.Fatalf("notice lost cache: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"suppressed_capture"}`))}, nil
	})}
	for _, send := range []func() error{
		func() error { return SpeakQueueReplyForTurn("reply", runID, 1) },
		func() error { return SpeakCachedForTurn("quota notice", runID) },
	} {
		if err := send(); !errors.Is(err, ErrCaptureActive) || errors.Is(err, ErrSpeakerMuted) {
			t.Fatalf("capture suppression lost its distinct result: %v", err)
		}
	}
}
