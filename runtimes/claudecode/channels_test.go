package claudecode

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestClaudeCodeSupportedChannels(t *testing.T) {
	got := (&ClaudeCodeService{}).SupportedChannels()
	if len(got) != 3 || got[0] != domain.ChannelTelegram || got[1] != domain.ChannelSlack || got[2] != domain.ChannelDiscord {
		t.Fatalf("SupportedChannels() = %v, want [telegram slack discord]", got)
	}
}

// slack is device-owned (slack.go): creds are read live from Device config,
// so AddChannel/RefreshChannelConfig are honest no-op successes that must NOT
// touch the presync/bridge-restart path.
func TestClaudeCodeAddChannelSlackNoOp(t *testing.T) {
	s := &ClaudeCodeService{}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"}); err != nil {
		t.Errorf("AddChannel(slack) err = %v, want nil (creds consumed live)", err)
	}
	if _, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelSlack}); err != nil {
		t.Errorf("RefreshChannelConfig(slack) err = %v, want nil (creds consumed live)", err)
	}
}

func TestClaudeCodeAddChannelUnsupported(t *testing.T) {
	s := &ClaudeCodeService{}
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestClaudeCodeRefreshChannelConfigUnsupported(t *testing.T) {
	s := &ClaudeCodeService{}
	_, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}
