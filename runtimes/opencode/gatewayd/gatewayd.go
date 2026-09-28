// Package gatewayd bridges a local WebSocket to per-turn `opencode run
// --format json` subprocesses. It runs as `os-server opencode-gatewayd` under
// the opencode.service systemd unit (EnvironmentFile=/root/.opencode/.env).
//
// Protocol (client = os-server runtimes/opencode):
//
//	client -> gatewayd: {"type":"message.send","id":..,"payload":{"content":..,
//	                     "attachments":[{"type":"image","url":"data:<mt>;base64,<b64>"}]}}
//	                    {"type":"session.new"}  -> forget session (runs after queued
//	                     turns), next turn is fresh
//	                    {"type":"ping","id":X}  -> {"type":"pong","id":X}
//	gatewayd -> client: opencode `run --format json` JSONL events forwarded
//	                    VERBATIM (text/reasoning/tool_use/step_start/step_finish/
//	                    message.updated/session.idle/session.error/..), plus
//	                    {"type":"pong"}, {"type":"bridge.status",..} and
//	                    {"type":"bridge.error","error":".."}.
//
// Turns are strictly serialized (buffered channel + single worker goroutine).
// The sessionID field present on every opencode JSONL line is persisted to the
// session file and replayed via `opencode run --session <id>` on subsequent
// turns. Model/provider come from opencode.json (presync-owned) — never --model.
package gatewayd

import (
	"context"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const (
	logPrefix     = "[opencode-gatewayd]"
	streamLimit   = 8 * 1024 * 1024 // max stdout line size (bufio.Scanner cap)
	scanBufSize   = 1024 * 1024     // initial bufio.Scanner buffer
	stderrTailMax = 4000            // bounded stderr tail kept for diagnostics
	attachMaxAge  = time.Hour       // best-effort cleanup of old attachments
	turnQueueCap  = 32              // pending worker ops (message.send / session.new)
)

// resumeErrHints are case-insensitive substrings in stderr/stdout meaning the
// resumed opencode session no longer exists (so a fresh retry is warranted).
// ⚠️ VERIFY ON DEVICE: opencode's verbatim missing-session error string.
var resumeErrHints = []string{"session not found", "no session", "not found", "unknown session"}

// Config holds every tunable. Main() fills it from environment variables
// (read once at start); tests construct it directly with temp paths.
type Config struct {
	JevConfigPath string
	JevEnabled    bool
	Token         string        // OPENCODE_WS_TOKEN
	Port          string        // OPENCODE_PORT (Main only; tests inject a Listener)
	Workspace     string        // OPENCODE_WORKSPACE
	Bin           string        // OPENCODE_BIN
	SessionFile   string        // OPENCODE_SESSION_FILE
	AttachDir     string        // OPENCODE_ATTACH_DIR
	TurnTimeout   time.Duration // OPENCODE_TURN_TIMEOUT_S
	Home          string        // HOME asserted into the subprocess env
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func configFromEnv() Config {
	timeout := 600 * time.Second
	if f, err := strconv.ParseFloat(envOr("OPENCODE_TURN_TIMEOUT_S", "600"), 64); err == nil && f > 0 {
		timeout = time.Duration(f * float64(time.Second))
	}
	return Config{
		JevConfigPath: envOr("JEV_CONFIG_PATH", "/root/config/config.json"),
		JevEnabled:    jevEnabled,
		Token:         envOr("OPENCODE_WS_TOKEN", "autonomous_opencode_token"),
		Port:          envOr("OPENCODE_PORT", "18793"),
		Workspace:     envOr("OPENCODE_WORKSPACE", "/root/.opencode/workspace"),
		Bin:           envOr("OPENCODE_BIN", "opencode"),
		SessionFile:   envOr("OPENCODE_SESSION_FILE", "/root/.opencode/session.json"),
		AttachDir:     envOr("OPENCODE_ATTACH_DIR", "/root/.opencode/attachments"),
		TurnTimeout:   timeout,
		Home:          "/root",
	}
}

// Server bridges a single WebSocket client to per-turn opencode subprocesses.
type Server struct {
	preloadContext  func(context.Context, string) string
	lifetimeContext context.Context
	activeRequestID string // guarded by mu
	activeRunID     string // guarded by mu
	cfg             Config
	ln              net.Listener

	mu       sync.Mutex // guards client, threadID and session-file writes
	client   *wsClient  // single client; a new connection replaces the old
	threadID string     // current opencode thread id ("" = fresh next turn)

	ops chan op // turns + session.new, strictly serialized by the single worker
}

// New builds a Server with explicit config and listener (tests use port 0).
func New(cfg Config, ln net.Listener) *Server {
	return &Server{
		preloadContext:  newPreloader(cfg),
		lifetimeContext: context.Background(),
		cfg:             cfg,
		ln:              ln,
		ops:             make(chan op, turnQueueCap),
	}
}

// Serve blocks until ctx is cancelled or the listener fails. It owns the
// turn-worker goroutine; on ctx cancellation any in-flight subprocess is
// killed (process group) and open connections are dropped.
func (s *Server) Serve(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	s.lifetimeContext = ctx

	if err := os.MkdirAll(s.cfg.Workspace, 0o755); err != nil {
		log.Printf("%s mkdir workspace failed: %v", logPrefix, err)
	}
	if err := os.MkdirAll(s.cfg.AttachDir, 0o755); err != nil {
		log.Printf("%s mkdir attach dir failed: %v", logPrefix, err)
	}
	s.threadID = s.loadSession()

	go s.turnWorker(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/opencode/ws", s.handleWS)
	mux.HandleFunc("/opencode/ws/", s.handleWS)
	httpSrv := &http.Server{Handler: mux}

	errCh := make(chan error, 1)
	go func() { errCh <- httpSrv.Serve(s.ln) }()
	log.Printf("%s listening on ws://%s/opencode/ws/", logPrefix, s.ln.Addr())

	select {
	case <-ctx.Done():
		_ = httpSrv.Close()
		<-errCh
		return nil
	case err := <-errCh:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}

// Main is the blocking entry point for `os-server opencode-gatewayd`. It reads
// config from the environment, listens on 127.0.0.1:OPENCODE_PORT and shuts
// down gracefully on SIGTERM/SIGINT.
func Main() int {
	cfg := configFromEnv()
	ln, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", cfg.Port))
	if err != nil {
		log.Printf("%s listen failed: %v", logPrefix, err)
		return 1
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := New(cfg, ln).Serve(ctx); err != nil {
		log.Printf("%s serve failed: %v", logPrefix, err)
		return 1
	}
	log.Printf("%s shut down", logPrefix)
	return 0
}
