package codex

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestCodexSupportedChannels(t *testing.T) {
	got := (&CodexService{}).SupportedChannels()
	if len(got) != 3 || got[0] != domain.ChannelTelegram || got[1] != domain.ChannelSlack || got[2] != domain.ChannelDiscord {
		t.Fatalf("SupportedChannels() = %v, want [telegram slack discord] (device-owned paths)", got)
	}
}

func TestCodexAddChannel(t *testing.T) {
	s := &CodexService{}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelTelegram}); err != nil {
		t.Errorf("AddChannel(telegram) err = %v, want nil (creds in config.json drive the device-owned loop)", err)
	}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelSlack, SlackBotToken: "x"}); err != nil {
		t.Errorf("AddChannel(slack) err = %v, want nil (creds in config.json drive the bridge)", err)
	}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{
		Channel: domain.ChannelDiscord, DiscordBotToken: "x", DiscordUserID: "42",
	}); err != nil {
		t.Errorf("AddChannel(discord) err = %v, want nil (creds in config.json drive the bot session)", err)
	}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelDiscord, DiscordBotToken: "x"}); err == nil {
		t.Errorf("AddChannel(discord) without user id must fail (receive path is allowlist-gated)")
	}
	if err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelDiscord, DiscordUserID: "42"}); err == nil {
		t.Errorf("AddChannel(discord) without bot token must fail")
	}
	err := s.AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelWhatsapp, WhatsappUserID: "x"})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("AddChannel(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestCodexRefreshChannelConfig(t *testing.T) {
	s := &CodexService{}
	out, err := s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelTelegram})
	if err != nil || out != "" {
		t.Errorf(`RefreshChannelConfig(telegram) = (%q, %v), want ("", nil)`, out, err)
	}
	out, err = s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelSlack})
	if err != nil || out != "" {
		t.Errorf(`RefreshChannelConfig(slack) = (%q, %v), want ("", nil)`, out, err)
	}
	out, err = s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelDiscord})
	if err != nil || out != "" {
		t.Errorf(`RefreshChannelConfig(discord) = (%q, %v), want ("", nil)`, out, err)
	}
	_, err = s.RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Errorf("RefreshChannelConfig(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}
