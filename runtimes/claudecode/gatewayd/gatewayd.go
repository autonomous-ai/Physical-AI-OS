// Package gatewayd bridges a local WebSocket to ONE persistent headless
// `claude` subprocess.
package gatewayd

import (
	"context"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"go.autonomous.ai/os/runtimes/claudecode"
)

const (
	logPrefix     = "[claudecode-gatewayd]"
	streamLimit   = 8 * 1024 * 1024 // max stdout line / WS message size
	scanBufSize   = 1024 * 1024     // initial bufio.Scanner buffer
	stderrTailMax = 4000            // bounded stderr tail kept for crash logging

	// defaultPort MUST match the port baked into claudecode.WSURL
	// (ws://127.0.0.1:18791/claude/ws/) — the client side of this socket.
	defaultPort = "18791"
)

// Config holds every tunable.
type Config struct {
	JevConfigPath  string
	JevEnabled     bool
	Token          string        // CLAUDECODE_WS_TOKEN
	Port           string        // CLAUDECODE_PORT (Main only; tests inject a Listener)
	Workspace      string        // CLAUDECODE_WORKSPACE (claude cwd)
	EnvFile        string        // CLAUDECODE_ENV_FILE (KEY=VALUE pairs merged into the child env)
	SessionFile    string        // CLAUDECODE_SESSION_FILE
	ClaudeBin      string        // CLAUDECODE_BIN
	RestartBackoff time.Duration // CLAUDECODE_RESTART_BACKOFF_S
	Home           string        // HOME asserted into the subprocess env
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func configFromEnv() Config {
	backoff := 5 * time.Second
	if f, err := strconv.ParseFloat(envOr("CLAUDECODE_RESTART_BACKOFF_S", "5"), 64); err == nil && f > 0 {
		backoff = time.Duration(f * float64(time.Second))
	}
	home := envOr("CLAUDECODE_HOME", "/root/.claudecode")
	return Config{
		JevConfigPath: envOr("JEV_CONFIG_PATH", "/root/config/config.json"),
		JevEnabled:    jevEnabled,
		// Token defaults to runtimes/claudecode/constants.go Token — the two
		// sides of the socket MUST agree.
		Token:          envOr("CLAUDECODE_WS_TOKEN", claudecode.Token),
		Port:           envOr("CLAUDECODE_PORT", defaultPort),
		Workspace:      envOr("CLAUDECODE_WORKSPACE", home+"/workspace"),
		EnvFile:        envOr("CLAUDECODE_ENV_FILE", home+"/.env"),
		SessionFile:    envOr("CLAUDECODE_SESSION_FILE", home+"/session.json"),
		ClaudeBin:      envOr("CLAUDECODE_BIN", "claude"),
		RestartBackoff: backoff,
		Home:           "/root",
	}
}

// Server bridges a single WebSocket client to the persistent claude child.
type Server struct {
	preloadContext  func(context.Context, string) string
	lifetimeContext context.Context
	cfg             Config
	ln              net.Listener

	// stdinMu serializes writes to the child stdin pipe. Lock order: stdinMu -> mu; it is held
	// across blocking pipe writes, so never acquire it while holding mu.
	stdinMu sync.Mutex

	mu         sync.Mutex     // guards everything below (never held across pipe writes)
	client     *wsClient      // single client; a new connection replaces the old
	sessionID  string         // current claude session id ("" = none yet)
	resumeNext bool           // pass --resume on the next spawn (cleared by session.new)
	child      *exec.Cmd      // running claude child (nil while down)
	stdin      io.WriteCloser // child stdin pipe (nil while down)
	pending    [][]byte       // stdin lines queued while the child is down
	inflight   int            // message.send frames not yet answered by a result event
}

// New builds a Server with explicit config and listener (tests use port 0).
func New(cfg Config, ln net.Listener) *Server {
	return &Server{cfg: cfg, ln: ln, preloadContext: newPreloader(cfg), lifetimeContext: context.Background()}
}

// Serve blocks until ctx is cancelled or the listener fails.
func (s *Server) Serve(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	s.lifetimeContext = ctx

	if err := os.MkdirAll(s.cfg.Workspace, 0o755); err != nil {
		log.Printf("%s mkdir workspace failed: %v", logPrefix, err)
	}
	s.sessionID = s.loadSession()
	s.resumeNext = s.sessionID != ""

	childDone := make(chan struct{})
	go func() {
		defer close(childDone)
		s.childLoop(ctx)
	}()
	// Do not return until the child loop has reaped its process group.
	defer func() {
		cancel()
		<-childDone
	}()

	mux := http.NewServeMux()
	mux.HandleFunc("/claude/ws", s.handleWS)
	mux.HandleFunc("/claude/ws/", s.handleWS)
	httpSrv := &http.Server{Handler: mux}

	errCh := make(chan error, 1)
	go func() { errCh <- httpSrv.Serve(s.ln) }()
	log.Printf("%s listening on ws://%s/claude/ws/", logPrefix, s.ln.Addr())

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

// Main is the blocking entry point for `os-server claudecode-gatewayd`.
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
