package httpapi

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type stubStatus struct{}

func (stubStatus) Status() Status { return Status{State: "idle"} }

type stubApproval struct{}

func (stubApproval) Approve(string) error { return nil }
func (stubApproval) Deny(string) error    { return nil }

type stubActivity struct{}

func (stubActivity) Notify(string, string, string, bool)  {}
func (stubActivity) Usage(int, int, string, string, bool) {}

// stubCode mirrors the real CodeApprovals long-poll semantics (channel per id).
type stubCode struct {
	mu           sync.Mutex
	items        map[string]chan string
	forceTimeout bool
}

func newStubCode() *stubCode { return &stubCode{items: map[string]chan string{}} }

func (s *stubCode) Request(ctx context.Context, req CodeApprovalRequest) (string, error) {
	if s.forceTimeout {
		return "timeout", nil
	}
	ch := make(chan string, 1)
	s.mu.Lock()
	s.items[req.ID] = ch
	s.mu.Unlock()
	select {
	case d := <-ch:
		return d, nil
	case <-ctx.Done():
		return "timeout", ctx.Err()
	case <-time.After(3 * time.Second):
		return "timeout", nil
	}
}

func (s *stubCode) Approve(id string) error { return s.set(id, "allow") }
func (s *stubCode) Deny(id string) error    { return s.set(id, "deny") }

func (s *stubCode) set(id, d string) error {
	s.mu.Lock()
	ch := s.items[id]
	s.mu.Unlock()
	if ch == nil {
		return ErrNoPending
	}
	ch <- d
	return nil
}

func (s *stubCode) Pending() []CodeApprovalRequest { return nil }

// allowAuth admits every request; tokenAuth admits only a fixed secret.
type allowAuth struct{}

func (allowAuth) Authorize(string) bool { return true }

type tokenAuth string

func (t tokenAuth) Authorize(secret string) bool { return secret == string(t) }

func newTestServer(code CodeApprovalService) *httptest.Server {
	s := New(0, allowAuth{}, stubStatus{}, stubApproval{}, stubActivity{}, code)
	return httptest.NewServer(s.routes())
}

func post(t *testing.T, url, body string) (*http.Response, string) {
	t.Helper()
	resp, err := http.Post(url, "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

// The approval long-poll returns the decision posted to /approve.
func TestApprovalRequest_AllowViaApprove(t *testing.T) {
	code := newStubCode()
	ts := newTestServer(code)
	defer ts.Close()

	done := make(chan string, 1)
	go func() {
		_, body := post(t, ts.URL+"/claude-code/approval-request",
			`{"id":"abc","tool":"Bash","input":{"command":"npm test"}}`)
		done <- body
	}()

	// Let the request register, then approve it (loopback via httptest).
	time.Sleep(200 * time.Millisecond)
	resp, body := post(t, ts.URL+"/claude-code/approve", `{"id":"abc"}`)
	if resp.StatusCode != 200 {
		t.Fatalf("approve status=%d body=%s", resp.StatusCode, body)
	}

	select {
	case got := <-done:
		var r struct{ Decision string }
		if err := json.Unmarshal([]byte(got), &r); err != nil {
			t.Fatalf("decode %q: %v", got, err)
		}
		if r.Decision != "allow" {
			t.Fatalf("decision=%q want allow", r.Decision)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("approval-request did not return after approve")
	}
}

func TestApprovalRequest_Timeout(t *testing.T) {
	code := newStubCode()
	code.forceTimeout = true
	ts := newTestServer(code)
	defer ts.Close()

	_, body := post(t, ts.URL+"/claude-code/approval-request", `{"id":"x","tool":"Bash"}`)
	if !strings.Contains(body, `"timeout"`) {
		t.Fatalf("want timeout decision, got %s", body)
	}
}

func TestApprovalRequest_BadInput(t *testing.T) {
	ts := newTestServer(newStubCode())
	defer ts.Close()

	if resp, _ := post(t, ts.URL+"/claude-code/approval-request", `not json`); resp.StatusCode != 400 {
		t.Fatalf("bad json status=%d want 400", resp.StatusCode)
	}
	if resp, _ := post(t, ts.URL+"/claude-code/approval-request", `{"tool":"Bash"}`); resp.StatusCode != 400 {
		t.Fatalf("missing id status=%d want 400", resp.StatusCode)
	}
}

func TestApprove_UnknownID_Conflict(t *testing.T) {
	ts := newTestServer(newStubCode())
	defer ts.Close()
	if resp, _ := post(t, ts.URL+"/claude-code/deny", `{"id":"nope"}`); resp.StatusCode != 409 {
		t.Fatalf("unknown id status=%d want 409", resp.StatusCode)
	}
}

func TestApprove_NonLoopbackForbidden(t *testing.T) {
	s := New(0, allowAuth{}, stubStatus{}, stubApproval{}, stubActivity{}, newStubCode())
	req := httptest.NewRequest("POST", "/claude-code/approve", strings.NewReader(`{"id":"abc"}`))
	req.RemoteAddr = "203.0.113.7:5555" // non-loopback
	rec := httptest.NewRecorder()
	s.routes().ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("non-loopback approve status=%d want 403", rec.Code)
	}
}

// LAN endpoints require the admin Bearer token.
func TestGuard_RequiresAdminToken(t *testing.T) {
	s := New(0, tokenAuth("s3cret"), stubStatus{}, stubApproval{}, stubActivity{}, newStubCode())
	ts := httptest.NewServer(s.routes())
	defer ts.Close()

	// No header → 401.
	resp, _ := post(t, ts.URL+"/claude-code/notify", `{"title":"hi"}`)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("no-token notify status=%d want 401", resp.StatusCode)
	}

	// Wrong token → 401.
	req, _ := http.NewRequest("POST", ts.URL+"/claude-code/notify", strings.NewReader(`{"title":"hi"}`))
	req.Header.Set("Authorization", "Bearer nope")
	resp, _ = mustDo(t, req)
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong-token notify status=%d want 401", resp.StatusCode)
	}

	// Correct token → not 401 (handler runs).
	req, _ = http.NewRequest("POST", ts.URL+"/claude-code/notify", strings.NewReader(`{"title":"hi"}`))
	req.Header.Set("Authorization", "Bearer s3cret")
	resp, _ = mustDo(t, req)
	if resp.StatusCode == http.StatusUnauthorized {
		t.Fatalf("correct-token notify got 401, want it admitted")
	}

	// /health stays open (discovery) — no token needed.
	hresp, err := http.Get(ts.URL + "/health")
	if err != nil {
		t.Fatalf("GET /health: %v", err)
	}
	hresp.Body.Close()
	if hresp.StatusCode != http.StatusOK {
		t.Fatalf("health status=%d want 200", hresp.StatusCode)
	}
}

func mustDo(t *testing.T, req *http.Request) (*http.Response, string) {
	t.Helper()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("do %s: %v", req.URL, err)
	}
	b, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	return resp, string(b)
}

func TestIsLoopback(t *testing.T) {
	cases := map[string]bool{
		"127.0.0.1:5002": true,
		"[::1]:5002":     true,
		"203.0.113.7:80": false,
		"192.168.1.5:80": false,
	}
	for addr, want := range cases {
		req := httptest.NewRequest("POST", "/x", nil)
		req.RemoteAddr = addr
		if got := isLoopback(req); got != want {
			t.Errorf("isLoopback(%q)=%v want %v", addr, got, want)
		}
	}
}
