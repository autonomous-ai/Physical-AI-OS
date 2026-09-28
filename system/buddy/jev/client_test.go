package jev

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type jevRoundTripFunc func(*http.Request) (*http.Response, error)

func (f jevRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

var testJevCandidates = []Candidate{{ID: "dim", Description: "Decrease this device's lamp brightness by one fixed step."}}

const testJevEndpoint = "https://proxy.example.test/ai/v1/jev/decisions"

const validJevResponse = `{"answers":{"intent":{"type":"choice","choice":"dim","probabilities":{"dim":0.96,"none":0.04}},"fit_dim":{"type":"noul","noul":0.99}}}`

func TestJevClientRequest(t *testing.T) {
	calls := 0
	client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != http.MethodPost || r.URL.String() != testJevEndpoint || r.Header.Get("Authorization") != "Bearer test-key" {
			t.Fatal("incorrect endpoint, method or authorization")
		}
		var request struct {
			Model string `json:"model"`
			State struct {
				Prompt     string      `json:"prompt"`
				Candidates []Candidate `json:"candidates"`
			} `json:"state"`
			Questions map[string]jevQuestion `json:"questions"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != jevModel || request.State.Prompt != "Chói quá" || len(request.State.Candidates) != 1 || len(request.Questions) != 2 {
			t.Fatalf("unexpected request: %+v", request)
		}
		if request.Questions["intent"].Type != "choice" || len(request.Questions["intent"].Criteria) != 2 || request.Questions["fit_dim"].Type != "noul" || !strings.Contains(request.Questions["fit_dim"].Instructions, "id dim") {
			t.Fatal("missing explicit choice or independent fit question")
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(validJevResponse))}, nil
	})}}
	got, err := client.decide(context.Background(), testJevEndpoint, "test-key", "Chói quá", testJevCandidates)
	if err != nil || got != "dim" || calls != 1 {
		t.Fatalf("got %q, %v; calls=%d", got, err, calls)
	}
}

func TestJevClientDecisions(t *testing.T) {
	cases := []struct {
		name, response, want string
		wantError            bool
	}{
		{"accept", validJevResponse, "dim", false},
		{"low_probability", strings.Replace(validJevResponse, `0.96,"none":0.04`, `0.89,"none":0.11`, 1), "", false},
		{"low_fit", strings.Replace(validJevResponse, "0.99", "0.94", 1), "", false},
		{"none", strings.Replace(validJevResponse, `"choice":"dim"`, `"choice":"none"`, 1), "", false},
		{"wrong_type", strings.Replace(validJevResponse, `"type":"choice"`, `"type":"noul"`, 1), "", true},
		{"unknown_choice", strings.Replace(validJevResponse, `"choice":"dim"`, `"choice":"shell"`, 1), "", true},
		{"missing_probability", strings.Replace(validJevResponse, `,"none":0.04`, "", 1), "", true},
		{"extra_probability", strings.Replace(validJevResponse, `"none":0.04`, `"none":0.04,"shell":0`, 1), "", true},
		{"unknown_probability", strings.Replace(validJevResponse, `"none":0.04`, `"shell":0.04`, 1), "", true},
		{"null_probability", strings.Replace(validJevResponse, `"none":0.04`, `"none":null`, 1), "", true},
		{"negative_probability", strings.Replace(validJevResponse, "0.04", "-0.04", 1), "", true},
		{"unnormalized", strings.Replace(validJevResponse, "0.96", "0.99", 1), "", true},
		{"null_distribution", `{"answers":{"intent":{"type":"choice","choice":"dim","probabilities":null}}}`, "", true},
		{"null_fit", strings.Replace(validJevResponse, "0.99", "null", 1), "", true},
		{"missing_fit", strings.Replace(validJevResponse, `,"fit_dim":{"type":"noul","noul":0.99}`, "", 1), "", true},
		{"wrong_fit_type", strings.Replace(validJevResponse, `"type":"noul"`, `"type":"choice"`, 1), "", true},
		{"fit_out_of_range", strings.Replace(validJevResponse, "0.99", "1.01", 1), "", true},
		{"error_envelope", strings.Replace(validJevResponse, `{"answers":`, `{"error":{"message":"secret"},"answers":`, 1), "", true},
		{"null", "null", "", true},
		{"malformed", "{", "", true},
		{"trailing_json", validJevResponse + "{}", "", true},
		{"too_large", validJevResponse + strings.Repeat(" ", jevMaxResponseBytes), "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(tc.response))}, nil
			})}}
			got, err := client.decide(context.Background(), testJevEndpoint, "key", "dim please", testJevCandidates)
			if got != tc.want || (err != nil) != tc.wantError {
				t.Fatalf("got %q, %v", got, err)
			}
		})
	}
}

func TestJevClientInvalidInputMakesNoRequest(t *testing.T) {
	client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("unexpected HTTP request")
		return nil, errors.New("unexpected request")
	})}}
	cases := []struct {
		key, text  string
		candidates []Candidate
	}{
		{"", "dim", testJevCandidates}, {"key", " ", testJevCandidates}, {"key", strings.Repeat("x", 8001), testJevCandidates},
		{"key", "dim", nil}, {"key", "dim", []Candidate{{ID: "none", Description: "invalid"}}},
		{"key", "dim", []Candidate{{ID: "bad.id", Description: "invalid"}}},
		{"key", "dim", []Candidate{{ID: "dim", Description: ""}}},
		{"key", "dim", append(append([]Candidate{}, testJevCandidates...), testJevCandidates...)},
	}
	for _, tc := range cases {
		if got, err := client.decide(context.Background(), testJevEndpoint, tc.key, tc.text, tc.candidates); err == nil || got != "" {
			t.Fatalf("got %q, %v", got, err)
		}
	}
}

func TestJevClientInvalidEndpointMakesNoRequest(t *testing.T) {
	client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("invalid endpoint reached HTTP transport")
		return nil, errors.New("unexpected request")
	})}}
	for _, endpoint := range []string{
		"", "/ai/v1/jev/decisions", "//proxy.example.test/ai/v1/jev/decisions",
		"https:", "https:///ai/v1/jev/decisions", "http://proxy.example.test/ai/v1/jev/decisions",
		"http://192.168.1.2/ai/v1/jev/decisions", "http://localhost.example.test/jev/decisions",
		"ftp://proxy.example.test/jev/decisions", "https://secret@proxy.example.test/jev/decisions",
		"https://proxy.example.test/jev/decisions?token=secret", "https://proxy.example.test/jev/decisions?",
		"https://proxy.example.test/jev/decisions#secret", "https://proxy.example.test/jev/decisions#",
	} {
		got, err := client.decide(context.Background(), endpoint, "key", "dim", testJevCandidates)
		if got != "" || err == nil || strings.Contains(err.Error(), "secret") {
			t.Fatalf("invalid endpoint returned %q, %v", got, err)
		}
	}
}

func TestJevEndpointAllowsHTTPSAndLoopbackHTTP(t *testing.T) {
	for _, endpoint := range []string{testJevEndpoint, "http://127.0.0.1:1234/jev/decisions", "http://[::1]:1234/jev/decisions", "http://localhost:1234/jev/decisions"} {
		if !validJevEndpoint(endpoint) {
			t.Fatalf("rejected valid endpoint %q", endpoint)
		}
	}
}

func TestJevClientFailureDoesNotLeak(t *testing.T) {
	for _, status := range []int{401, 402, 429, 500} {
		calls := 0
		client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
			calls++
			return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("secret upstream text"))}, nil
		})}}
		_, err := client.decide(context.Background(), testJevEndpoint, "secret-key", "secret prompt", testJevCandidates)
		if err == nil || strings.Contains(err.Error(), "secret") || calls != 1 {
			t.Fatalf("error=%v calls=%d", err, calls)
		}
	}
	client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("secret transport details")
	})}}
	_, err := client.decide(context.Background(), testJevEndpoint, "secret-key", "secret prompt", testJevCandidates)
	if err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("error=%v", err)
	}
}

func TestJevClientCancellation(t *testing.T) {
	for _, timeout := range []bool{false, true} {
		client := jevClient{httpClient: &http.Client{Transport: jevRoundTripFunc(func(r *http.Request) (*http.Response, error) {
			<-r.Context().Done()
			return nil, r.Context().Err()
		})}}
		ctx, cancel := context.WithCancel(context.Background())
		want := context.Canceled
		if timeout {
			cancel()
			ctx, cancel = context.WithTimeout(context.Background(), time.Millisecond)
			want = context.DeadlineExceeded
		} else {
			cancel()
		}
		_, err := client.decide(ctx, testJevEndpoint, "key", "dim", testJevCandidates)
		cancel()
		if !errors.Is(err, want) {
			t.Fatalf("error=%v, want %v", err, want)
		}
	}
}

func TestJevClientRejectsRedirects(t *testing.T) {
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetCalls.Add(1)
		w.Write([]byte(validJevResponse))
	}))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusTemporaryRedirect)
	}))
	defer redirect.Close()
	for _, injected := range []*http.Client{nil, redirect.Client()} {
		client := jevClient{httpClient: injected}
		_, err := client.decide(context.Background(), redirect.URL, "secret-key", "dim", testJevCandidates)
		if err == nil || targetCalls.Load() != 0 {
			t.Fatalf("error=%v target calls=%d", err, targetCalls.Load())
		}
	}
}

func TestJevProbabilityMustBeFinite(t *testing.T) {
	for _, p := range []float64{math.NaN(), math.Inf(1), math.Inf(-1), -1, 2} {
		if validJevProbability(&p) {
			t.Fatalf("accepted %v", p)
		}
	}
}

func TestJevLocalProxyFailuresFallThroughAndDoNotRetry(t *testing.T) {
	for _, slow := range []bool{false, true} {
		t.Run(fmt.Sprint("slow=", slow), func(t *testing.T) {
			var calls atomic.Int32
			proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls.Add(1)
				if r.URL.Path != "/api/v1/ai/v1/jev/decisions" || r.Header.Get("Authorization") != "Bearer device-key" {
					t.Error("incorrect proxy contract")
				}
				if slow {
					select {
					case <-r.Context().Done():
					case <-time.After(Budget + time.Second):
					}
					return
				}
				w.WriteHeader(http.StatusNotFound) // BFF has not deployed the proposed route.
			}))
			defer proxy.Close()
			resolver := New(proxy.Client())
			opts := Options{Enabled: true, Endpoint: proxy.URL + "/api/v1/ai/v1/jev/decisions", APIKey: "device-key"}
			started := time.Now()
			if got := resolver.Suggest(context.Background(), "Press Continue", fixtureTree(), opts); got.Suggestion != nil {
				t.Fatalf("failed request selected %+v", got)
			}
			if time.Since(started) >= Budget+500*time.Millisecond {
				t.Fatal("proxy failure exceeded bounded fallback budget")
			}
			if resolver.Suggest(context.Background(), "Press Continue", fixtureTree(), opts).Suggestion != nil || calls.Load() != 1 {
				t.Fatal("cooldown retried local proxy")
			}
		})
	}
}
