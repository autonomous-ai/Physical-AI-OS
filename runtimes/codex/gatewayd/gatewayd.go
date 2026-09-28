// Package gatewayd bridges a local WebSocket to a persistent Codex App Server.
//
// Protocol (client = os-server runtimes/codex):
//
//	client -> gatewayd: {"type":"message.send","id":..,"payload":{"content":..,
//	                     "attachments":[{"type":"image","url":"data:<mt>;base64,<b64>"}]}}
//	                    {"type":"session.new"}  -> forget thread (runs after queued
//	                     turns), next turn is fresh
//	                    {"type":"ping","id":X}  -> {"type":"pong","id":X}
//	gatewayd -> client: codex `--json` JSONL events forwarded VERBATIM
//	                    (thread.started/item.*/turn.completed/turn.failed/..),
//	                    plus {"type":"pong"}, {"type":"bridge.status",..} and
//	                    {"type":"bridge.error","error":".."}.
//
// A message received while a turn is active is submitted with turn/steer. This
// is intentionally different from the old per-turn codex exec worker: it lets
// direct voice and web-chat input join the current model turn instead of
// waiting behind it. Passive sensing stays subject to the OS safety queue.
package gatewayd

import (
	"context"
	"encoding/json"
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
	logPrefix     = "[codex-gatewayd]"
	streamLimit   = 8 * 1024 * 1024 // max stdout line size (bufio.Scanner cap)
	scanBufSize   = 1024 * 1024     // initial bufio.Scanner buffer
	stderrTailMax = 4000            // bounded stderr tail kept for diagnostics
	attachMaxAge  = time.Hour       // best-effort cleanup of old attachments
	turnQueueCap  = 32              // pending worker ops (message.send / session.new)
)

// resumeErrHints are case-insensitive substrings in stderr/stdout meaning the
// resumed thread/session no longer exists (so a fresh retry is warranted).
// "no rollout found" is codex rust-v0.142.5's verbatim missing-thread error
// ("thread/resume failed: no rollout found for thread id <id>").
var resumeErrHints = []string{"no rollout found", "no conversation", "not found", "session"}

// Config holds every tunable. Main() fills it from environment variables
// (read once at start); tests construct it directly with temp paths.
type Config struct {
	JevConfigPath string
	JevEnabled    bool
	Token         string        // CODEX_WS_TOKEN
	Port          string        // CODEX_PORT (Main only; tests inject a Listener)
	Workspace     string        // CODEX_WORKSPACE
	CodexBin      string        // CODEX_BIN
	CodexHome     string        // CODEX_HOME
	SessionFile   string        // CODEX_SESSION_FILE
	AttachDir     string        // CODEX_ATTACH_DIR
	TurnTimeout   time.Duration // CODEX_TURN_TIMEOUT_S
	Home          string        // HOME asserted into the subprocess env
	UseAppServer  bool          // false = stable codex exec JSONL path
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func configFromEnv() Config {
	timeout := 600 * time.Second
	if f, err := strconv.ParseFloat(envOr("CODEX_TURN_TIMEOUT_S", "600"), 64); err == nil && f > 0 {
		timeout = time.Duration(f * float64(time.Second))
	}
	// CODEX_HOME anchors the per-file defaults, so setting it alone relocates
	// the whole state dir (the client side resolves the same var via syspath).
	home := envOr("CODEX_HOME", "/root/.codex")
	return Config{
		JevConfigPath: envOr("JEV_CONFIG_PATH", "/root/config/config.json"),
		JevEnabled:    jevEnabled,
		Token:         envOr("CODEX_WS_TOKEN", "autonomous_codex_token"),
		Port:          envOr("CODEX_PORT", "18792"),
		Workspace:     envOr("CODEX_WORKSPACE", home+"/workspace"),
		CodexBin:      envOr("CODEX_BIN", "codex"),
		CodexHome:     home,
		SessionFile:   envOr("CODEX_SESSION_FILE", home+"/session.json"),
		AttachDir:     envOr("CODEX_ATTACH_DIR", home+"/attachments"),
		TurnTimeout:   timeout,
		Home:          envOr("OS_AGENT_HOME", "/root"),
		UseAppServer:  envOr("CODEX_APP_SERVER", "1") != "0",
	}
}

// Server bridges a single WebSocket client to per-turn codex subprocesses.
type Server struct {
	preloadContext  func(context.Context, string) string
	lifetimeContext context.Context
	cfg             Config
	ln              net.Listener

	mu                sync.Mutex  // guards client, threadID and session-file writes
	client            *wsClient   // single client; a new connection replaces the old
	threadID          string      // current codex thread id ("" = fresh next turn)
	activeRequestID   string      // guarded by mu; worker-owned turn correlation
	activeRunID       string      // originating device run, carried through queued turns
	activeTurnID      string      // current App Server turn; non-empty means steer
	activeStarting    bool        // thread/turn start RPC is in flight
	resetPending      bool        // session.new received during an active turn
	appTurnTimer      *time.Timer // bounded by CODEX_TURN_TIMEOUT_S
	appTurnTimedOut   bool
	appOutputRejected bool
	app               *appServer
	// appUsage holds the newest thread/tokenUsage/updated block until the
	// turn's terminal event attaches it (App Server reports usage on its own
	// notification, never on turn/completed).
	appUsage json.RawMessage

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

	if s.cfg.UseAppServer {
		app, err := startAppServer(ctx, s)
		if err != nil {
			return err
		}
		s.mu.Lock()
		s.app = app
		s.mu.Unlock()
		defer app.close()
	} else {
		log.Printf("%s using per-turn codex exec (App Server disabled)", logPrefix)
	}
	go s.turnWorker(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/codex/ws", s.handleWS)
	mux.HandleFunc("/codex/ws/", s.handleWS)
	httpSrv := &http.Server{Handler: mux}

	errCh := make(chan error, 1)
	go func() { errCh <- httpSrv.Serve(s.ln) }()
	log.Printf("%s listening on ws://%s/codex/ws/", logPrefix, s.ln.Addr())

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

// Main is the blocking entry point for `os-server codex-gatewayd`. It reads
// config from the environment, listens on 127.0.0.1:CODEX_PORT and shuts
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
