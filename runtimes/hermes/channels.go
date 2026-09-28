package hermes

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// SupportedChannels — Hermes Agent (Nous Research) delivers telegram/slack/discord/
// imessage natively inside its own server; a channel is enabled by the presence
// of its tokens in ~/.hermes/.env (Slack uses Socket Mode → SLACK_APP_TOKEN;
// iMessage rides on the BlueBubbles plugin → BLUEBUBBLES_SERVER_URL etc.).
// os-server's only job is to land creds in .env and bounce the gateway — it
// runs no channel receive loop of its own. WhatsApp pairing (Baileys) is
// OpenClaw-only, so it is not listed here.
func (s *HermesService) SupportedChannels() []string {
	return []string{
		domain.ChannelTelegram,
		domain.ChannelSlack,
		domain.ChannelDiscord,
		domain.ChannelIMessage,
	}
}

// AddChannel re-syncs ~/.hermes/.env from config.json and restarts hermes-gateway
// only when the config actually changed. The device layer persists the channel creds
// to config.json BEFORE calling this (persist-then-apply), so the presync run sees
// them. Unsupported channels return domain.ErrChannelNotSupported.
func (s *HermesService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("hermes: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	slog.Info("hermes channel add: re-syncing .env", "component", "hermes", "channel", channel)
	return s.syncChannelsEnv()
}

// RefreshChannelConfig re-applies the channel's .env mapping (config-only path,
// mirrors AddChannel here since both reduce to "re-sync .env + restart-if-changed").
// Returns "" for the runtime version string (the active version surfaces via
// Version()); unsupported channels return domain.ErrChannelNotSupported.
func (s *HermesService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("hermes: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	slog.Info("hermes channel refresh: re-syncing .env", "component", "hermes", "channel", req.Channel)
	return "", s.syncChannelsEnv()
}

// syncChannelsEnv runs the embedded presync hook (which upserts every non-empty
// config.json channel var into ~/.hermes/.env) and restarts hermes-gateway only when
// config.yaml or .env changed — reusing EnsureOnboarding's hash-diff restart logic so
// there is no second copy of the sync/restart rules.
func (s *HermesService) syncChannelsEnv() error {
	return s.EnsureOnboarding()
}
