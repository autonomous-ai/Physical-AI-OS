package opencode

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"

	"go.autonomous.ai/os/system/domain"
)

func (s *OpenCodeService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw.
func (s *OpenCodeService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on opencode backend",
	}
	close(ch)
	return ch
}

// RestartAgent restarts the opencode gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it.
func (s *OpenCodeService) RestartAgent() error {
	slog.Info("RestartAgent: restarting opencode gateway", "component", "opencode")
	return restartOpenCodeGateway()
}

// RefreshModelsConfig — OpenCode model config (opencode.json model/provider/
// apiKey) is owned by presync.sh.
func (s *OpenCodeService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh).

// FetchChatHistory — OpenCode history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's synchronous dispatch
// deadlock-free since the handler never blocks on a WS RPC).
func (s *OpenCodeService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns opencode's global config (~/.config/opencode/opencode.json)
// verbatim for the gw-config UI.
// Unlike codex (TOML) this is real JSON and safe to expose — the provider apiKey is a
// `{env:LLM_API_KEY}` reference, the real secret lives only in .env.
func (s *OpenCodeService) GetConfigJSON() (json.RawMessage, error) {
	raw, err := os.ReadFile(opencodeConfigJSON)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, domain.ErrNotSupportedByRuntime
		}
		return nil, fmt.Errorf("read opencode config: %w", err)
	}
	if !json.Valid(raw) {
		return nil, fmt.Errorf("opencode config is not valid JSON")
	}
	return json.RawMessage(raw), nil
}

// StartModelSync — model registry is owned by OpenCode.
func (s *OpenCodeService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the OpenCode model is pinned in opencode.json by presync
// (from config.json llm_model), not patched per-call.
func (s *OpenCodeService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *OpenCodeService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under OpenCode.
func (s *OpenCodeService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}
