package codex

import (
	"context"
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

func (s *CodexService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw.
func (s *CodexService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on codex backend",
	}
	close(ch)
	return ch
}

// RestartAgent restarts the codex gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it.
func (s *CodexService) RestartAgent() error {
	slog.Info("RestartAgent: restarting codex gateway", "component", "codex")
	return restartCodexGateway()
}

// RefreshModelsConfig — Codex model config (config.toml model/base_url/env
// key) is owned by presync.sh.
func (s *CodexService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh).

// FetchChatHistory — Codex history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's synchronous dispatch
// deadlock-free since the handler never blocks on a WS RPC).
func (s *CodexService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — Codex's config is TOML (/root/.codex/config.toml), not
// JSON, and its secrets ride in .env; there is no JSON config file to expose.
func (s *CodexService) GetConfigJSON() (json.RawMessage, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

// StartModelSync — model registry is owned by Codex.
func (s *CodexService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the Codex model is pinned in config.toml by presync
// (from config.json llm_model), not patched per-call.
func (s *CodexService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *CodexService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under Codex.
func (s *CodexService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}
