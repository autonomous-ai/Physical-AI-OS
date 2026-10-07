package device

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
	"unicode"

	"go.autonomous.ai/os/runtimes/accountlogin"
	"go.autonomous.ai/os/system/server/config"
)

var ErrRuntimeLoginConflict = errors.New("runtime account login is busy or the session is no longer available")

type RuntimeLoginSession struct {
	ID            string `json:"id"`
	Runtime       string `json:"runtime"`
	Provider      string `json:"provider"`
	Status        string `json:"status"`
	LoginURL      string `json:"login_url,omitempty"`
	UserCode      string `json:"user_code,omitempty"`
	InputRequired bool   `json:"input_required,omitempty"`
	Error         string `json:"error,omitempty"`
}
type RuntimeLoginStatus struct {
	Runtime   string                  `json:"runtime"`
	Providers []accountlogin.Provider `json:"providers"`
	Session   *RuntimeLoginSession    `json:"session,omitempty"`
}
type runtimeLoginState struct {
	mu      sync.Mutex
	session *RuntimeLoginSession
	cancel  context.CancelFunc
	input   chan string
	// Injection seam for isolated native-process tests; production uses Prepare.
	prepare func(string, string, string) (*accountlogin.Flow, error)
}

func loginOngoing(status string) bool {
	return status == "starting" || status == "waiting" || status == "applying"
}

func (s *Service) GetRuntimeLogin() RuntimeLoginStatus {
	current := s.CurrentAgentRuntime()
	status := RuntimeLoginStatus{Runtime: current, Providers: accountlogin.Providers(current)}
	s.runtimeLogin.mu.Lock()
	defer s.runtimeLogin.mu.Unlock()
	if session := s.runtimeLogin.session; session != nil && session.Runtime == current && !(session.Status == "success" && !s.config.LLMRuntimeManaged()) {
		copy := *session
		status.Session = &copy
	}
	return status
}

func (s *Service) StartRuntimeLogin(runtime, provider string) (RuntimeLoginSession, error) {
	if !s.runtimeSwitchMu.TryLock() {
		return RuntimeLoginSession{}, ErrAgentRuntimeSwitchInProgress
	}
	release := true
	defer func() {
		if release {
			s.runtimeSwitchMu.Unlock()
		}
	}()
	if runtime != s.CurrentAgentRuntime() {
		return RuntimeLoginSession{}, fmt.Errorf("select and switch to the runtime before connecting an account")
	}
	allowed := false
	for _, p := range accountlogin.Providers(runtime) {
		if p.ID == provider {
			allowed = true
		}
	}
	if !allowed {
		return RuntimeLoginSession{}, fmt.Errorf("unsupported runtime account provider")
	}
	if s.agentGateway == nil {
		return RuntimeLoginSession{}, fmt.Errorf("runtime is unavailable")
	}
	s.runtimeLogin.mu.Lock()
	defer s.runtimeLogin.mu.Unlock()
	if s.runtimeLogin.session != nil && loginOngoing(s.runtimeLogin.session.Status) {
		return RuntimeLoginSession{}, ErrRuntimeLoginConflict
	}
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return RuntimeLoginSession{}, fmt.Errorf("create login session: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	session := RuntimeLoginSession{ID: hex.EncodeToString(id), Runtime: runtime, Provider: provider, Status: "starting"}
	s.runtimeLogin.session = &session
	s.runtimeLogin.cancel = cancel
	s.runtimeLogin.input = make(chan string, 1)
	input := s.runtimeLogin.input
	prepare := s.runtimeLogin.prepare
	if prepare == nil {
		prepare = accountlogin.Prepare
	}
	release = false
	go s.runRuntimeLogin(ctx, session, input, prepare)
	return session, nil
}

func (s *Service) SubmitRuntimeLoginCode(id, code string) (RuntimeLoginSession, error) {
	code = strings.TrimSpace(code)
	if code == "" || len(code) > 8192 || strings.IndexFunc(code, unicode.IsControl) >= 0 {
		return RuntimeLoginSession{}, fmt.Errorf("enter a valid sign-in code or callback URL")
	}
	s.runtimeLogin.mu.Lock()
	defer s.runtimeLogin.mu.Unlock()
	session := s.runtimeLogin.session
	if session == nil || session.ID != id || session.Status != "waiting" || !session.InputRequired {
		return RuntimeLoginSession{}, ErrRuntimeLoginConflict
	}
	select {
	case s.runtimeLogin.input <- code:
		return *session, nil
	default:
		return RuntimeLoginSession{}, ErrRuntimeLoginConflict
	}
}

func (s *Service) CancelRuntimeLogin(id string) (RuntimeLoginSession, error) {
	s.runtimeLogin.mu.Lock()
	defer s.runtimeLogin.mu.Unlock()
	session := s.runtimeLogin.session
	if session == nil || session.ID != id || !loginOngoing(session.Status) || session.Status == "applying" {
		return RuntimeLoginSession{}, ErrRuntimeLoginConflict
	}
	// Keep the active state until the worker has killed and reaped the CLI.
	s.runtimeLogin.cancel()
	return *session, nil
}

func (s *Service) loginUpdate(id string, update func(*RuntimeLoginSession)) {
	s.runtimeLogin.mu.Lock()
	defer s.runtimeLogin.mu.Unlock()
	if s.runtimeLogin.session != nil && s.runtimeLogin.session.ID == id {
		update(s.runtimeLogin.session)
	}
}

func (s *Service) runRuntimeLogin(ctx context.Context, session RuntimeLoginSession, input <-chan string, prepare func(string, string, string) (*accountlogin.Flow, error)) {
	// Unlock before publishing terminal status, so a retry can start immediately.
	status, message := "error", "Could not prepare native sign-in. Check that the runtime is installed and try again."
	defer func() {
		s.runtimeLogin.mu.Lock()
		if s.runtimeLogin.cancel != nil {
			s.runtimeLogin.cancel()
		}
		s.runtimeSwitchMu.Unlock()
		s.runtimeLogin.session.Status = status
		s.runtimeLogin.session.Error = message
		s.runtimeLogin.session.LoginURL = ""
		s.runtimeLogin.session.UserCode = ""
		s.runtimeLogin.session.InputRequired = false
		s.runtimeLogin.mu.Unlock()
	}()
	flow, err := prepare("/root", session.Runtime, session.Provider)
	if err != nil {
		return
	}
	defer flow.Close()
	err = flow.Run(ctx, func(prompt accountlogin.Prompt) {
		s.loginUpdate(session.ID, func(current *RuntimeLoginSession) {
			current.Status = "waiting"
			current.LoginURL = prompt.URL
			current.UserCode = prompt.UserCode
			current.InputRequired = prompt.InputRequired
		})
	}, input)
	if ctx.Err() != nil {
		if errors.Is(ctx.Err(), context.Canceled) {
			status, message = "cancelled", ""
		} else {
			message = "Sign-in expired. Start again to get a new link and code."
		}
		return
	}
	if err != nil {
		message = "Native sign-in did not complete. Check your account permissions and try again."
		return
	}
	verifyCtx, cancel := context.WithTimeout(ctx, 60*time.Second)
	err = flow.Verify(verifyCtx)
	cancel()
	if err != nil {
		message = "Could not verify the new account. Your current AI configuration has not been changed."
		return
	}
	// Once applying, cancellation is disabled; finish or roll back as one operation.
	s.runtimeLogin.mu.Lock()
	if ctx.Err() != nil {
		s.runtimeLogin.mu.Unlock()
		status, message = "cancelled", ""
		return
	}
	s.runtimeLogin.session.Status = "applying"
	s.runtimeLogin.session.LoginURL = ""
	s.runtimeLogin.session.UserCode = ""
	s.runtimeLogin.session.InputRequired = false
	s.runtimeLogin.mu.Unlock()
	if err = s.activateRuntimeLogin(flow); err != nil {
		message = err.Error()
		return
	}
	status, message = "success", ""
}

func (s *Service) activateRuntimeLogin(flow *accountlogin.Flow) error {
	previous := s.config.LLMMode()
	// Stop OS reconciliation from overwriting newly installed native fields.
	if err := s.config.WithLockSave(func(c *config.Config) { c.LLMConfigMode = "runtime" }); err != nil {
		if restoreErr := s.config.WithLockSave(func(c *config.Config) { c.LLMConfigMode = previous }); restoreErr != nil {
			return fmt.Errorf("Could not save account mode or restore the saved mode. Check device storage before retrying.")
		}
		return fmt.Errorf("Could not save account mode. Your existing account was not changed.")
	}
	rollback, err := flow.Install()
	if err == nil {
		err = s.applyLLMMode()
	}
	if err == nil && !s.WaitForAgentReadyStable(90*time.Second, 15*time.Second) {
		err = fmt.Errorf("runtime did not become ready")
	}
	if err == nil {
		return nil
	}
	var restoreErr error
	if rollback != nil {
		restoreErr = rollback()
	}
	restoreErr = errors.Join(restoreErr, s.config.WithLockSave(func(c *config.Config) { c.LLMConfigMode = previous }))
	restoreErr = errors.Join(restoreErr, s.applyLLMMode())
	if restoreErr != nil {
		return fmt.Errorf("Account activation failed and restoring the previous configuration also failed. Check the runtime before retrying.")
	}
	return fmt.Errorf("Account activation failed. The previous AI configuration was restored; try again.")
}
