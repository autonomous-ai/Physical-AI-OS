package device

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/runtimes/accountlogin"
	"go.autonomous.ai/os/system/domain"
)

type loginGateway struct{ llmModeGateway }

func (*loginGateway) IsReady() bool { return true }

func awaitLogin(t *testing.T, s *Service, status string) RuntimeLoginSession {
	t.Helper()
	deadline := time.After(4 * time.Second)
	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-deadline:
			t.Fatalf("did not reach %s: %+v", status, s.GetRuntimeLogin())
		case <-ticker.C:
			if state := s.GetRuntimeLogin(); state.Session != nil && state.Session.Status == status {
				return *state.Session
			}
		}
	}
}
func TestAccountLoginCancellationKeepsOSAndSerializes(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	cfg.AgentRuntime = "claudecode"
	cfg.LLMConfigMode = "os"
	svc := &Service{config: cfg, agentGateway: &loginGateway{}}
	svc.runtimeLogin.prepare = func(_, _, _ string) (*accountlogin.Flow, error) {
		return &accountlogin.Flow{
			Command: exec.Command("sh", "-c", `printf 'https://claude.ai/oauth/authorize\n'; read code`), Hosts: []string{"claude.ai"}, InputRequired: true,
			Verify: func(context.Context) error { t.Error("cancelled login verified"); return nil }, Install: func() (func() error, error) { t.Error("cancelled login installed"); return nil, nil }, Close: func() {},
		}, nil
	}
	started, err := svc.StartRuntimeLogin("claudecode", "anthropic")
	if err != nil {
		t.Fatal(err)
	}
	awaitLogin(t, svc, "waiting")
	if cfg.LLMMode() != "os" {
		t.Fatal("OS disabled before login")
	}
	if _, err = svc.StartRuntimeLogin("claudecode", "anthropic"); !errors.Is(err, ErrAgentRuntimeSwitchInProgress) {
		t.Fatalf("second login: %v", err)
	}
	if _, err = svc.ReserveAgentRuntimeSwitch(domain.AgentRuntimeSetData{Runtime: "hermes"}); !errors.Is(err, ErrAgentRuntimeSwitchInProgress) {
		t.Fatalf("switch during login: %v", err)
	}
	if _, err = svc.SubmitRuntimeLoginCode("wrong", "abc"); !errors.Is(err, ErrRuntimeLoginConflict) {
		t.Fatal("stale input accepted")
	}
	if _, err = svc.SubmitRuntimeLoginCode(started.ID, "abc\ncommand"); err == nil {
		t.Fatal("control input accepted")
	}
	if _, err = svc.CancelRuntimeLogin(started.ID); err != nil {
		t.Fatal(err)
	}
	awaitLogin(t, svc, "cancelled")
	if cfg.LLMMode() != "os" {
		t.Fatal("cancel changed mode")
	}
	if !svc.runtimeSwitchMu.TryLock() {
		t.Fatal("cancel did not release runtime")
	}
	svc.runtimeSwitchMu.Unlock()
}
func TestAccountLoginFailureDoesNotAdoptOldCredentials(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	cfg.AgentRuntime = "codex"
	cfg.LLMConfigMode = "os"
	svc := &Service{config: cfg, agentGateway: &loginGateway{}}
	svc.runtimeLogin.prepare = func(_, _, _ string) (*accountlogin.Flow, error) {
		return &accountlogin.Flow{Command: exec.Command("sh", "-c", "exit 0"), Verify: func(context.Context) error { return errors.New("missing fresh auth secret-must-not-leak") }, Install: func() (func() error, error) { t.Error("unverified install"); return nil, nil }, Close: func() {}}, nil
	}
	if _, err := svc.StartRuntimeLogin("codex", "openai"); err != nil {
		t.Fatal(err)
	}
	session := awaitLogin(t, svc, "error")
	if strings.Contains(session.Error, "secret-must-not-leak") || cfg.LLMMode() != "os" {
		t.Fatal("failure leaked error or changed OS mode")
	}
}
func TestAccountActivationRollback(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	cfg.LLMConfigMode = "os"
	gw := &loginGateway{llmModeGateway: llmModeGateway{onboardErr: errors.New("apply failed")}}
	svc := &Service{config: cfg, agentGateway: gw}
	restored := false
	err := svc.activateRuntimeLogin(&accountlogin.Flow{Install: func() (func() error, error) { return func() error { restored = true; return nil }, nil }})
	if err == nil || !restored || cfg.LLMMode() != "os" || gw.refreshes != 1 {
		t.Fatalf("rollback failed: %v mode=%s restored=%v", err, cfg.LLMMode(), restored)
	}
	svc.runtimeLogin.session = &RuntimeLoginSession{Runtime: "openclaw", Status: "success"}
	cfg.AgentRuntime = "openclaw"
	if svc.GetRuntimeLogin().Session != nil {
		t.Fatal("stale success shown after returning to OS")
	}
}

func TestAccountLoginVerifiedApplyAndSuccess(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	cfg.AgentRuntime = "claudecode"
	cfg.LLMConfigMode = "os"
	svc := &Service{config: cfg, agentGateway: &loginGateway{}}
	verified, installed := false, false
	svc.runtimeLogin.prepare = func(_, _, _ string) (*accountlogin.Flow, error) {
		return &accountlogin.Flow{
			Command: exec.Command("sh", "-c", `printf 'https://claude.com/cai/oauth/authorize\n'; read code; test "$code" = 'code#state'`), Hosts: []string{"claude.com"}, InputRequired: true,
			Verify: func(context.Context) error { verified = true; return nil }, Install: func() (func() error, error) {
				if !verified {
					t.Error("installed before validation")
				}
				installed = true
				return func() error { return nil }, nil
			}, Close: func() {},
		}, nil
	}
	session, err := svc.StartRuntimeLogin("claudecode", "anthropic")
	if err != nil {
		t.Fatal(err)
	}
	awaitLogin(t, svc, "waiting")
	if _, err = svc.SubmitRuntimeLoginCode(session.ID, "code#state"); err != nil {
		t.Fatal(err)
	}
	deadline := time.After(25 * time.Second)
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-deadline:
			t.Fatal("verified account did not finish activation")
		case <-ticker.C:
			state := svc.GetRuntimeLogin()
			if state.Session != nil && state.Session.Status == "success" {
				if !installed || cfg.LLMMode() != "runtime" || cfg.LLMAPIKey != "key-llm" || cfg.LLMModel != "model-a" {
					t.Fatal("login lost original OS credentials")
				}
				if state.Session.LoginURL != "" || state.Session.InputRequired {
					t.Fatal("completed session retained sign-in prompt")
				}
				return
			}
		}
	}
}
