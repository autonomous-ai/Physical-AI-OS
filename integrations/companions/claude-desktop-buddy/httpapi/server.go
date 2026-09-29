// Package httpapi is the HTTP delivery layer for the buddy daemon.
package httpapi

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"time"
)

// maxBodyBytes caps request bodies; these endpoints only take small JSON.
const maxBodyBytes = 64 << 10

// Server routes requests to its injected ports.
type Server struct {
	port          int
	startAt       time.Time
	auth          Authenticator
	status        StatusProvider
	approvals     ApprovalService
	activity      ActivitySink
	codeApprovals CodeApprovalService
}

// New builds the delivery layer from its ports.
func New(port int, auth Authenticator, status StatusProvider, approvals ApprovalService, activity ActivitySink, codeApprovals CodeApprovalService) *Server {
	return &Server{
		port:          port,
		startAt:       time.Now(),
		auth:          auth,
		status:        status,
		approvals:     approvals,
		activity:      activity,
		codeApprovals: codeApprovals,
	}
}

// Start serves HTTP on the configured port.
func (s *Server) Start() error {
	addr := fmt.Sprintf(":%d", s.port)
	log.Printf("[http] listening on %s", addr)
	srv := &http.Server{
		Addr:              addr,
		Handler:           s.routes(),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       15 * time.Second,
		// WriteTimeout stays 0: /claude-code/approval-request long-polls ~55s.
		WriteTimeout: 0,
		IdleTimeout:  60 * time.Second,
	}
	return srv.ListenAndServe()
}

// routes registers all endpoints.
func (s *Server) routes() http.Handler {
	mux := http.NewServeMux()

	// Open (no token) so plugin discovery can find the device.
	mux.HandleFunc("GET /health", s.handleHealth)

	// Loopback-only: read by the on-device agent.
	mux.HandleFunc("GET /status", s.loopbackOnly(s.handleStatus))

	// Loopback-only: the on-device agent relays Desktop approvals.
	mux.HandleFunc("POST /claude-desktop/approve", s.loopbackOnly(s.handleApprove))
	mux.HandleFunc("POST /claude-desktop/deny", s.loopbackOnly(s.handleDeny))

	// LAN plugin pushes: admin-token gated.
	mux.HandleFunc("POST /claude-code/notify", s.guard(s.handleNotify))
	mux.HandleFunc("POST /claude-code/usage", s.guard(s.handleUsage))

	// Approval long-poll is admin-token gated; approve/deny/pending are loopback-only.
	mux.HandleFunc("POST /claude-code/approval-request", s.guard(s.handleApprovalRequest))
	mux.HandleFunc("POST /claude-code/approve", s.handleCodeApprove)
	mux.HandleFunc("POST /claude-code/deny", s.handleCodeDeny)
	mux.HandleFunc("GET /claude-code/pending", s.loopbackOnly(s.handleCodePending))

	return mux
}

// decodeJSON reads a size-capped JSON body into v.
func decodeJSON(w http.ResponseWriter, r *http.Request, v any) error {
	return json.NewDecoder(io.LimitReader(r.Body, maxBodyBytes)).Decode(v)
}

// guard requires a valid `Authorization: Bearer <admin-password>`; fails closed (401) without an Authenticator.
func (s *Server) guard(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if s.auth == nil || !s.auth.Authorize(token) {
			fail(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		h(w, r)
	}
}

// loopbackOnly rejects non-loopback callers with 403.
func (s *Server) loopbackOnly(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !isLoopback(r) {
			fail(w, http.StatusForbidden, "loopback-only")
			return
		}
		h(w, r)
	}
}

// isLoopback reports whether the request came from the local host.
func isLoopback(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// ok / fail emit the uniform {"ok":bool,...} envelope used by every endpoint.
func ok(w http.ResponseWriter) {
	writeJSON(w, http.StatusOK, map[string]interface{}{"ok": true})
}

func fail(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]interface{}{"ok": false, "error": msg})
}
