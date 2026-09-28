package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/harness"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
)

// TestHarnessLiveLocalBridge is explicitly opt-in. It exposes only the existing
// signed device socket to LAN; test control endpoints require actual loopback.
// It never sends a task by itself. Voice routes require an explicit local HAL
// test fixture so the normal HAL endpoint cannot be contacted accidentally.
type harnessLiveHALTransport struct {
	base   http.RoundTripper
	target *url.URL
}

func (t harnessLiveHALTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL.Host == "127.0.0.1:5001" {
		r = r.Clone(r.Context())
		copyURL := *r.URL
		r.URL = &copyURL
		r.URL.Scheme, r.URL.Host = t.target.Scheme, t.target.Host
		r.Host = t.target.Host
	}
	return t.base.RoundTrip(r)
}

func TestHarnessLiveLocalBridge(t *testing.T) {
	dir := os.Getenv("OS_HARNESS_LIVE_DIR")
	if dir == "" {
		t.Skip("set OS_HARNESS_LIVE_DIR to an isolated private scratch directory")
	}
	voiceEnabled := false
	if endpoint := os.Getenv("OS_HARNESS_TEST_HAL_URL"); endpoint != "" {
		target, err := url.Parse(endpoint)
		if err != nil || target.Scheme != "http" || target.User != nil || target.RawQuery != "" || target.Fragment != "" || (target.Path != "" && target.Path != "/") {
			t.Fatal("HAL test endpoint must be a plain loopback HTTP origin")
		}
		ip := net.ParseIP(target.Hostname())
		if ip == nil || !ip.IsLoopback() || target.Port() == "" || target.Host == "127.0.0.1:5001" {
			t.Fatal("HAL test endpoint must be an explicit loopback fixture port")
		}
		previous := http.DefaultTransport
		http.DefaultTransport = harnessLiveHALTransport{base: previous, target: target}
		defer func() { http.DefaultTransport = previous }()
		voiceEnabled = true
	}
	if !filepath.IsAbs(dir) || filepath.Clean(dir) != dir {
		t.Fatal("live directory must be clean and absolute")
	}
	resolved, err := filepath.EvalSymlinks(dir)
	if err != nil || resolved != dir {
		t.Fatal("live directory must already exist without symlink aliases")
	}
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
		t.Fatal("live directory must have mode 0700")
	}
	writePrivate := func(name string, data []byte) error {
		f, err := os.CreateTemp(dir, ".live-")
		if err != nil {
			return err
		}
		defer os.Remove(f.Name())
		if _, err = f.Write(data); err == nil {
			err = f.Sync()
		}
		closeErr := f.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
		return os.Rename(f.Name(), filepath.Join(dir, name))
	}
	logFile, err := os.OpenFile(filepath.Join(dir, "events.jsonl"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer logFile.Close()
	var logMu sync.Mutex
	logEvent := func(frame harness.Frame) {
		logMu.Lock()
		defer logMu.Unlock()
		b, err := json.Marshal(frame)
		if err != nil {
			return
		}
		_, _ = logFile.Write(append(b, '\n'))
		_ = logFile.Sync()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Minute)
	defer cancel()
	s := &Server{agentHandler: &agenthttp.AgentHandler{}, harnessVoiceCtx: ctx}
	if err = s.initializeHarnessResults(filepath.Join(dir, "results.json")); err != nil {
		t.Fatal(err)
	}
	service, err := harness.NewService(dir, harness.Callbacks{
		BeforeRequest: s.reserveHarnessResult,
		BeforeEvent:   s.captureHarnessResult,
		OnReceipt: func(frame harness.Frame, peer harness.ResultContext) {
			logEvent(harness.Frame{"local": "receipt", "frame": frame})
			s.bindHarnessResultReceipt(frame, peer)
		},
		OnEvent: func(frame harness.Frame) { logEvent(frame); s.forwardHarnessEvent(frame) },
	})
	if err != nil {
		t.Fatal(err)
	}
	s.harnessService = service
	service.Start(ctx)
	workerDone := make(chan struct{})
	go func() { defer close(workerDone); s.watchHarnessResults(ctx) }()
	openPair := func() error {
		pair, err := service.StartPair(ctx)
		if err != nil {
			return err
		}
		return writePrivate("pair-code", []byte(pair.Code+"\n"))
	}
	if err = openPair(); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.Handle("/api/harness/ws", service)
	respond := func(w http.ResponseWriter, value any) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		_ = json.NewEncoder(w).Encode(value)
	}
	local := func(method string, next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			ip := net.ParseIP(host)
			if err != nil || ip == nil || !ip.IsLoopback() || r.Header.Get("Origin") != "" {
				http.Error(w, "loopback only", http.StatusForbidden)
				return
			}
			if r.Method != method {
				http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			next(w, r)
		}
	}
	mux.HandleFunc("/pair", local(http.MethodPost, func(w http.ResponseWriter, r *http.Request) {
		if err := openPair(); err != nil {
			http.Error(w, err.Error(), 409)
			return
		}
		respond(w, map[string]any{"pairing": true})
	}))
	mux.HandleFunc("/state", local(http.MethodGet, func(w http.ResponseWriter, r *http.Request) {
		status := service.Status()
		status.Code = ""
		s.harnessRepliesMu.Lock()
		pending := make([]string, 0, len(s.harnessReplies))
		for run := range s.harnessReplies {
			pending = append(pending, run)
		}
		s.harnessRepliesMu.Unlock()
		respond(w, map[string]any{"status": status, "pendingRunIds": pending, "results": s.harnessResults.Results(), "inputs": s.harnessResults.Inputs(), "answers": s.harnessResults.Answers(), "staged": len(s.harnessResults.Staged())})
	}))
	mux.HandleFunc("/command", local(http.MethodPost, func(w http.ResponseWriter, r *http.Request) {
		var frame harness.Frame
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 65536)).Decode(&frame); err != nil || frame == nil {
			http.Error(w, "invalid frame", 400)
			return
		}
		run, _ := frame["localRunId"].(string)
		delete(frame, "localRunId")
		channel, _ := frame["localChannel"].(string)
		delete(frame, "localChannel")
		if channel != "" && channel != "web" && channel != "voice" {
			http.Error(w, "invalid local channel", 400)
			return
		}
		if channel == "voice" && !voiceEnabled {
			http.Error(w, "voice requires OS_HARNESS_TEST_HAL_URL", 400)
			return
		}
		kind, _ := frame["type"].(string)
		agent, _ := frame["agentId"].(string)
		if kind == "turn.send" || kind == "question.answer" {
			if run == "" || agent == "" {
				http.Error(w, "mutation requires localRunId and agentId", 400)
				return
			}
			s.registerHarnessDispatch(agent, run, channel != "voice", true, frame)
		}
		requestCtx, stop := context.WithTimeout(r.Context(), 30*time.Second)
		defer stop()
		result, err := service.Request(requestCtx, frame)
		if err != nil {
			respond(w, map[string]any{"error": err.Error(), "frame": result})
			return
		}
		respond(w, map[string]any{"frame": result})
	}))
	mux.HandleFunc("/cancel-speech", local(http.MethodPost, func(w http.ResponseWriter, r *http.Request) {
		if !voiceEnabled {
			http.Error(w, "voice test fixture required", 400)
			return
		}
		s.agentHandler.CancelSpeech()
		respond(w, map[string]any{"cancelledAtMs": time.Now().UnixMilli()})
	}))
	stopped := make(chan struct{})
	var stopOnce sync.Once
	mux.HandleFunc("/stop", local(http.MethodPost, func(w http.ResponseWriter, r *http.Request) {
		respond(w, map[string]any{"stopping": true})
		stopOnce.Do(func() { close(stopped) })
	}))
	httpServer := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	serverDone := make(chan error, 1)
	go func() { serverDone <- httpServer.Serve(listener) }()
	ready, _ := json.Marshal(map[string]any{"port": listener.Addr().(*net.TCPAddr).Port, "pid": os.Getpid(), "deadline": time.Now().Add(12 * time.Minute).Format(time.RFC3339)})
	if err = writePrivate("ready.json", ready); err != nil {
		_ = httpServer.Close()
		t.Fatal(err)
	}
	fmt.Fprintln(os.Stdout, "Local Harness integration bridge ready; private control metadata written to scratch directory.")
	select {
	case <-stopped:
	case <-ctx.Done():
	case err := <-serverDone:
		if !errors.Is(err, http.ErrServerClosed) {
			t.Error(err)
		}
	}
	cancel()
	shutdownCtx, stopShutdown := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopShutdown()
	_ = httpServer.Shutdown(shutdownCtx)
	_ = httpServer.Close()
	<-workerDone
}
