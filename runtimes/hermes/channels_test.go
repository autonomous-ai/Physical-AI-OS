package hermes

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestHermesSupportedChannels(t *testing.T) {
	got := (&HermesService{}).SupportedChannels()
	want := map[string]bool{
		domain.ChannelTelegram: true,
		domain.ChannelSlack:    true,
		domain.ChannelDiscord:  true,
		domain.ChannelIMessage: true,
	}
	if len(got) != len(want) {
		t.Fatalf("SupportedChannels() = %v, want telegram/slack/discord/imessage", got)
	}
	for _, c := range got {
		if !want[c] {
			t.Errorf("SupportedChannels() includes unexpected %q", c)
		}
	}
}

func TestHermesAddChannelRejectsWhatsapp(t *testing.T) {
	err := (&HermesService{}).AddChannel(context.Background(), domain.AddChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Fatalf("AddChannel(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}

func TestHermesRefreshRejectsWhatsapp(t *testing.T) {
	_, err := (&HermesService{}).RefreshChannelConfig(context.Background(), domain.RefreshChannelRequest{Channel: domain.ChannelWhatsapp})
	if !errors.Is(err, domain.ErrChannelNotSupported) {
		t.Fatalf("RefreshChannelConfig(whatsapp) err = %v, want ErrChannelNotSupported", err)
	}
}
