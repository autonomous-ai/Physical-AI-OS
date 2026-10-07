package device

import (
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

type llmModeGateway struct {
	domain.AgentGateway
	refreshes, onboards, restarts      int
	refreshErr, onboardErr, restartErr error
}

func (g *llmModeGateway) RefreshModelsConfig() error { g.refreshes++; return g.refreshErr }
func (g *llmModeGateway) EnsureOnboarding() error    { g.onboards++; return g.onboardErr }
func (g *llmModeGateway) RestartAgent() error        { g.restarts++; return g.restartErr }

func TestNativeModePreservesOSCredentialsOnSettingsSave(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	cfg.LLMConfigMode = "runtime"
	gateway := &llmModeGateway{}
	svc := &Service{config: cfg, agentGateway: gateway}
	disabled := true
	if err := svc.UpdateConfig(domain.UpdateConfigRequest{
		DeviceID: "lamp", LLMAPIKey: "stale-key", LLMBaseURL: "https://stale.invalid",
		LLMModel: "stale-model", LLMDisableThinking: &disabled,
	}); err != nil {
		t.Fatal(err)
	}
	if cfg.LLMAPIKey != "key-llm" || cfg.LLMBaseURL != "https://api.example.com" || cfg.LLMModel != "model-a" || cfg.LLMThinkingDisabled() {
		t.Fatal("native mode changed the saved OS LLM configuration")
	}
	if cfg.DeviceID != "lamp" || gateway.refreshes != 0 || gateway.onboards != 0 || gateway.restarts != 0 {
		t.Fatalf("unrelated save did not stay independent: %+v", gateway)
	}
}

func TestLLMModeApplyAndRetry(t *testing.T) {
	for _, mode := range []string{"os", "runtime"} {
		t.Run(mode, func(t *testing.T) {
			t.Chdir(t.TempDir())
			cfg := baseConfig()
			cfg.LLMConfigMode = "runtime"
			failure := errors.New("presync failed")
			gateway := &llmModeGateway{refreshErr: domain.ErrNotSupportedByRuntime, onboardErr: failure}
			svc := &Service{config: cfg, agentGateway: gateway}
			req := domain.UpdateConfigRequest{LLMConfigMode: &mode}
			if err := svc.UpdateConfig(req); !errors.Is(err, failure) {
				t.Fatalf("apply failure lost: %v", err)
			}
			if gateway.restarts != 0 {
				t.Fatal("restarted after failed configuration")
			}
			gateway.onboardErr = nil
			if err := svc.UpdateConfig(req); err != nil {
				t.Fatal(err)
			}
			if gateway.onboards != 2 || gateway.restarts != 1 {
				t.Fatalf("same-mode retry was skipped: %+v", gateway)
			}
			if cfg.LLMMode() != mode {
				t.Fatal("mode not persisted")
			}
		})
	}
}

func TestExplicitOSReappliesUnchangedConfiguration(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	gateway := &llmModeGateway{}
	svc := &Service{config: cfg, agentGateway: gateway}
	mode := "os"
	for i := 0; i < 2; i++ {
		if err := svc.UpdateConfig(domain.UpdateConfigRequest{LLMConfigMode: &mode}); err != nil {
			t.Fatal(err)
		}
	}
	if gateway.refreshes != 2 {
		t.Fatalf("refreshes = %d", gateway.refreshes)
	}
}

func TestUnchangedThinkingDoesNotRefreshRuntime(t *testing.T) {
	t.Chdir(t.TempDir())
	gateway := &llmModeGateway{}
	svc := &Service{config: baseConfig(), agentGateway: gateway}
	disabled := false
	if err := svc.UpdateConfig(domain.UpdateConfigRequest{LLMDisableThinking: &disabled}); err != nil {
		t.Fatal(err)
	}
	if gateway.refreshes != 0 || gateway.onboards != 0 {
		t.Fatal("unchanged thinking triggered reconcile")
	}
}

func TestLLMModeRejectsInvalidAndConcurrentSwitch(t *testing.T) {
	t.Chdir(t.TempDir())
	cfg := baseConfig()
	svc := &Service{config: cfg}
	mode := "invalid"
	if err := svc.UpdateConfig(domain.UpdateConfigRequest{LLMConfigMode: &mode}); err == nil {
		t.Fatal("accepted invalid mode")
	}
	if cfg.LLMMode() != "" {
		t.Fatal("invalid mode persisted")
	}
	svc.runtimeSwitchMu.Lock()
	defer svc.runtimeSwitchMu.Unlock()
	mode = "runtime"
	if err := svc.UpdateConfig(domain.UpdateConfigRequest{LLMConfigMode: &mode}); !errors.Is(err, ErrAgentRuntimeSwitchInProgress) {
		t.Fatalf("concurrent switch error: %v", err)
	}
}
