package hermes

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// SupportedChannels: Hermes delivers channels natively; a channel is enabled by its tokens in ~/.hermes/.env.
func (s *HermesService) SupportedChannels() []string {
	return []string{
		domain.ChannelTelegram,
		domain.ChannelSlack,
		domain.ChannelDiscord,
		domain.ChannelIMessage,
	}
}

// AddChannel re-syncs ~/.hermes/.env from config.json and restarts hermes-gateway only when the config actually changed.
func (s *HermesService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("hermes: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	slog.Info("hermes channel add: re-syncing .env", "component", "hermes", "channel", channel)
	return s.syncChannelsEnv()
}

// RefreshChannelConfig re-applies the channel's .env mapping (config-only path, mirrors AddChannel here since both reduce to "re-sync .env + restart-if-changed").
func (s *HermesService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("hermes: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	slog.Info("hermes channel refresh: re-syncing .env", "component", "hermes", "channel", req.Channel)
	return "", s.syncChannelsEnv()
}

// syncChannelsEnv runs presync to upsert channel vars into .env and restarts the gateway only on change.
func (s *HermesService) syncChannelsEnv() error {
	return s.EnsureOnboarding()
}
