package server

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// A HAL restart is a listener that goes away and comes back on the same port.
func reviveAfter(t *testing.T, d time.Duration, h http.Handler) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := l.Addr().String()
	if err := l.Close(); err != nil { // dead: connections are refused
		t.Fatalf("close: %v", err)
	}
	done := make(chan struct{})
	go func() {
		time.Sleep(d)
		l2, err := net.Listen("tcp", addr)
		if err != nil {
			close(done)
			return
		}
		srv := &http.Server{Handler: h}
		t.Cleanup(func() { _ = srv.Close() })
		close(done)
		_ = srv.Serve(l2)
	}()
	t.Cleanup(func() { <-done })
	return "http://" + addr
}

// A POST that lands while HAL is restarting survives it.
func TestPiperFetchRetriesPostUntilHALReturns(t *testing.T) {
	var got []byte
	url := reviveAfter(t, 2*time.Second, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))

	body := []byte(`{"name":"vi_VN-vais1000-medium"}`)
	resp, err := piperFetch(context.Background(), http.MethodPost, url, body,
		time.Now().Add(10*time.Second))
	if err != nil {
		t.Fatalf("expected the retry to outlast the outage, got %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	if string(got) != string(body) {
		t.Fatalf("body reached HAL as %q, want %q", got, body)
	}
}

// Status polls fail immediately rather than being held open.
func TestPiperFetchDoesNotRetryGet(t *testing.T) {
	url := reviveAfter(t, 2*time.Second, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	start := time.Now()
	if _, err := piperFetch(context.Background(), http.MethodGet, url, nil,
		time.Now().Add(10*time.Second)); err == nil {
		t.Fatal("expected GET to fail fast while the listener is down")
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("GET took %v, expected it to give up at once", elapsed)
	}
}

// Only a refused dial is retried; a timeout is not.
func TestDialFailedSeparatesRefusalFromSlowReply(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	dead := l.Addr().String()
	_ = l.Close()
	if _, err := http.Get("http://" + dead); err == nil || !dialFailed(err) {
		t.Fatalf("a refused dial must be retriable, got %v", err)
	}

	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(300 * time.Millisecond)
	}))
	defer slow.Close()
	client := &http.Client{Timeout: 50 * time.Millisecond}
	if _, err := client.Get(slow.URL); err == nil || dialFailed(err) {
		t.Fatalf("a slow reply must not be replayed, got %v", err)
	}
}

// Once HAL answers, its reply is final, including a refusal.
func TestPiperFetchDoesNotRetryARefusal(t *testing.T) {
	var calls int
	url := reviveAfter(t, 0, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"error","message":"voice is in use"}`))
	}))
	time.Sleep(200 * time.Millisecond) // let it come up

	resp, err := piperFetch(context.Background(), http.MethodPost, url, []byte(`{}`),
		time.Now().Add(10*time.Second))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	defer resp.Body.Close()
	if calls != 1 {
		t.Fatalf("HAL was called %d times, want exactly 1", calls)
	}
}
