package picoclaw

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// SupportedChannels — PicoClaw runs telegram only: the Telegram receive loop is
// device-owned (driven by config.TelegramBotToken), and PicoClaw has no slack/discord
// delivery.
func (s *PicoclawService) SupportedChannels() []string {
	return []string{domain.ChannelTelegram}
}

// AddChannel — telegram is device-owned (creds in config.json drive the receive
// loop), so there is nothing to write into the runtime: an honest no-op.
func (s *PicoclawService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if channel != domain.ChannelTelegram {
		return fmt.Errorf("picoclaw: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	slog.Info("AddChannel: telegram is device-owned, no-op (picoclaw backend)", "component", "picoclaw")
	return nil
}

// RefreshChannelConfig — same capability rule.
func (s *PicoclawService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if req.Channel != domain.ChannelTelegram {
		return "", fmt.Errorf("picoclaw: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	return "", nil
}
