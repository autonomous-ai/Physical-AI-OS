package opencode

import (
	"context"
	"fmt"

	"go.autonomous.ai/os/system/domain"
)

// The OpenCode CLI has no channel layer, so os-server runs the inbound channel loops itself.

// SupportedChannels — telegram (device-owned receive loop, telegram_poll.go),
// slack (HTTP-mode proxy path, slack.go) and discord (gateway bot session,
// discord.go).
func (s *OpenCodeService) SupportedChannels() []string {
	return []string{domain.ChannelTelegram, domain.ChannelSlack, domain.ChannelDiscord}
}

// AddChannel — telegram, slack and discord are honest no-op successes: every
// consumer reads the creds fresh from Device config on each use (the telegram
// loop reads config.TelegramBotToken / TelegramUserID per iteration; the
// Slack bridge reads config.SlackUserID per event and config.SlackBotToken
// per Web API call; the discord bot reads config.DiscordBotToken per connect
// attempt and DiscordUserID / DiscordGuildID per message), so the creds the
// caller just persisted to config.json are all that is needed — there is
// nothing agent-side to write.
func (s *OpenCodeService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("opencode: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	if channel == domain.ChannelDiscord {
		if data.DiscordBotToken == "" {
			return fmt.Errorf("opencode: discord_bot_token is required")
		}
		if data.DiscordUserID == "" {
			return fmt.Errorf("opencode: discord_user_id is required")
		}
	}
	return nil
}

// RefreshChannelConfig — same rule: telegram/slack/discord creds are consumed
// live from Device config, so there is nothing to refresh and no derived
// value to return.
func (s *OpenCodeService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("opencode: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	return "", nil
}
