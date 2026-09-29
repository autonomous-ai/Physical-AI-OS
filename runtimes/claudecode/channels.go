package claudecode

import (
	"context"
	"fmt"

	"go.autonomous.ai/os/system/domain"
)

// SupportedChannels — all three channels are device-owned (mirroring runtimes/codex).
func (s *ClaudeCodeService) SupportedChannels() []string {
	return []string{domain.ChannelTelegram, domain.ChannelSlack, domain.ChannelDiscord}
}

// AddChannel — all supported channels are honest no-op successes: the
// device-owned loops read creds fresh from Device config on each use (the
// telegram poll loop per iteration, the discord bot per (re)connect attempt,
// the Slack bridge per event/Web API call), so the creds the caller just
// persisted (persist-then-apply) are all that is needed — nothing agent-side
// to write, no bridge restart.
func (s *ClaudeCodeService) AddChannel(_ context.Context, data domain.AddChannelRequest) error {
	channel := data.EffectiveChannel()
	if !domain.ChannelSupported(s, channel) {
		return fmt.Errorf("claudecode: channel %q: %w", channel, domain.ErrChannelNotSupported)
	}
	return nil
}

// RefreshChannelConfig — same capability rule and the same no-op contract as
// AddChannel.
func (s *ClaudeCodeService) RefreshChannelConfig(_ context.Context, req domain.RefreshChannelRequest) (string, error) {
	if !domain.ChannelSupported(s, req.Channel) {
		return "", fmt.Errorf("claudecode: channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	return "", nil
}
