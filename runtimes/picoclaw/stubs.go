package picoclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"

	"go.autonomous.ai/os/system/domain"
)

func (s *PicoclawService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw.
func (s *PicoclawService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on picoclaw backend",
	}
	close(ch)
	return ch
}

// RestartAgent restarts the picoclaw gateway via systemctl so callers that need a
// full gateway reload (config/workspace re-read) get it.
func (s *PicoclawService) RestartAgent() error {
	slog.Info("RestartAgent: restarting picoclaw gateway", "component", "picoclaw")
	return restartPicoclawGateway()
}

// RefreshModelsConfig — PicoClaw model config is owned by install.sh/presync.sh
// (switch-runtime flow); we don't patch it from Device.
func (s *PicoclawService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// EnsureOnboarding lives in onboarding.go — it keeps the OS-managed block in the
// workspace AGENTS.md current (the rest of provisioning is owned by install.sh /
// presync.sh).

// FetchChatHistory — PicoClaw history is server-side and we don't walk it.
// Returns empty so callers degrade gracefully (also keeps the read loop's synchronous dispatch
// deadlock-free since the handler never blocks on a WS RPC).
func (s *PicoclawService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns the raw bytes of PicoClaw's config.json (the structure
// file: agents/model_list/gateway/channel_list/tools — secrets live in .security.yml,
// which we never expose).
func (s *PicoclawService) GetConfigJSON() (json.RawMessage, error) {
	data, err := os.ReadFile(picoclawConfigPath())
	if err != nil {
		return nil, fmt.Errorf("read picoclaw config.json: %w", err)
	}
	return json.RawMessage(data), nil
}

// StartModelSync — model registry is owned by PicoClaw.
func (s *PicoclawService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the PicoClaw model registry (config.json model_list) is
// owned by the runtime's own provisioning, not device-selectable.
func (s *PicoclawService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no agent-side config file to watch.
func (s *PicoclawService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under PicoClaw.
func (s *PicoclawService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	return "channel"
}

// CompactSession — PicoClaw does not expose a compact API; rotate via
// NewSession instead.
func (s *PicoclawService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (picoclaw backend)", "component", "picoclaw", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

const rotateCompressRatioPercent = 75
const picoclawFallbackTokenThreshold = 150_000

// ShouldRotateSession rotates on real session token count (see domain.AgentGateway).
func (s *PicoclawService) ShouldRotateSession(totalTokens, _ int) bool {
	if compressAt := int(s.lastCompressAt.Load()); compressAt > 0 {
		return totalTokens >= compressAt*rotateCompressRatioPercent/100
	}
	return totalTokens > picoclawFallbackTokenThreshold
}

// NewSession — PicoClaw has no sessions.new RPC.
func (s *PicoclawService) NewSession(sessionKey string) error {
	slog.Info("NewSession: clearing session (picoclaw backend)", "component", "picoclaw", "key", sessionKey)
	s.sessionUUID.Store("")
	return nil
}
