package jev

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

var harnessCandidates = []Candidate{
	{ID: "session_0", Description: "Blender workspace /Projects/Airplane; task: create airplane"},
	{ID: "session_1", Description: "Blender workspace /Projects/House; task: create house and garden"},
}

const harnessDecision = `{"answers":{"intent":{"type":"choice","choice":"session_1","probabilities":{"session_0":0.01,"session_1":0.98,"none":0.01}},"fit_session_0":{"type":"noul","noul":0.01},"fit_session_1":{"type":"noul","noul":0.99}}}`

func TestHarnessResolverPreservesTaskAndUsesOwnershipBoundary(t *testing.T) {
	prompt := "Add trees to /Projects/House.\nOriginal Task: Create a House."
	resolver := NewHarnessResolver()
	resolver.client.(*jevClient).httpClient = &http.Client{Transport: jevRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		var payload struct {
			State struct {
				Prompt string `json:"prompt"`
			}
			Questions map[string]jevQuestion
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload.State.Prompt != prompt {
			t.Fatalf("task context altered: %q", payload.State.Prompt)
		}
		for name, q := range payload.Questions {
			if !strings.Contains(q.Instructions, "session") || !strings.Contains(q.Instructions, "untrusted") {
				t.Fatalf("missing session boundary for %s", name)
			}
			if strings.Contains(q.Instructions, "speaking desktop robot") || strings.Contains(q.Instructions, "accepted user intent") {
				t.Fatalf("hardware boundary leaked into %s", name)
			}
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(harnessDecision))}, nil
	})}
	got := resolver.Resolve(context.Background(), prompt, harnessCandidates, Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test"})
	if got.Intent != "session_1" {
		t.Fatalf("got %+v", got)
	}
}

func TestHarnessResolverRejectsUnknownAndAmbiguousDecisions(t *testing.T) {
	for _, body := range []string{
		strings.Replace(harnessDecision, `"choice":"session_1"`, `"choice":"invented"`, 1),
		strings.Replace(harnessDecision, `"choice":"session_1"`, `"choice":"none"`, 1),
		strings.Replace(harnessDecision, `"noul":0.99`, `"noul":0.7`, 1),
		`{invalid`,
	} {
		resolver := NewHarnessResolver()
		resolver.client.(*jevClient).httpClient = &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
		})}
		if got := resolver.Resolve(context.Background(), "Add trees", harnessCandidates, Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test"}); got.Intent != "" {
			t.Fatalf("accepted %s: %+v", body, got)
		}
	}
}

func TestHarnessResolverTimeoutAbstainsAndOpensCooldown(t *testing.T) {
	calls := 0
	resolver := NewHarnessResolver()
	resolver.client.(*jevClient).httpClient = &http.Client{Transport: jevRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		<-r.Context().Done()
		return nil, r.Context().Err()
	})}
	options := Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test", Timeout: time.Millisecond}
	for i := 0; i < 2; i++ {
		if got := resolver.Resolve(context.Background(), "Add trees", harnessCandidates, options); got.Intent != "" {
			t.Fatalf("got %+v", got)
		}
	}
	if calls != 1 {
		t.Fatalf("expected cooldown, calls=%d", calls)
	}
}

func TestHarnessResolverRejectsActionParametersBeforeRequest(t *testing.T) {
	resolver := NewHarnessResolver()
	resolver.client.(*jevClient).httpClient = &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("unexpected provider request")
		return nil, nil
	})}
	candidates := []Candidate{{ID: "session_0", Description: "House", Parameters: map[string]Parameter{"color": {Description: "Color", Options: []string{"red"}}}}}
	if got := resolver.Resolve(context.Background(), "Paint house red", candidates, Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test"}); got.Intent != "" {
		t.Fatalf("got %+v", got)
	}
}
