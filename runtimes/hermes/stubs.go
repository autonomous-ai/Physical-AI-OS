package hermes

import (
	"context"
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

func (s *HermesService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives only in OpenClaw.
func (s *HermesService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on hermes backend",
	}
	close(ch)
	return ch
}

// RefreshModelsConfig — Hermes config (~/.hermes/...) is owned externally; we don't patch it from Device.
func (s *HermesService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// FetchChatHistory is not supported; history lives server-side.
func (s *HermesService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON — no agent-side config file under Hermes (config.yaml is owned by presync, secrets live in .env).
func (s *HermesService) GetConfigJSON() (json.RawMessage, error) {
	return nil, domain.ErrNotSupportedByRuntime
}

// StartModelSync — model registry is owned by Hermes.
func (s *HermesService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel is a no-op: Hermes uses a fixed request model.
func (s *HermesService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no openclaw.json to watch.
func (s *HermesService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — Device config is the source of truth under Hermes.
func (s *HermesService) GetConfiguredChannel() string {
	if s.config.BluebubblesServerURL != "" && s.config.BluebubblesPassword != "" {
		return domain.ChannelIMessage
	}
	if s.config.TelegramBotToken != "" {
		return domain.ChannelTelegram
	}
	if s.config.SlackBotToken != "" {
		return domain.ChannelSlack
	}
	if s.config.DiscordBotToken != "" {
		return domain.ChannelDiscord
	}
	return "channel"
}

// CompactSession — Hermes does not currently expose a compact API or CLI (hermes.md §7 decided to no-op).
func (s *HermesService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (hermes backend)", "component", "hermes", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}
