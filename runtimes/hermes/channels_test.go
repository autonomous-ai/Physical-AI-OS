package hermes

import (
	"context"
	"errors"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestHermesSupportedChannels(t *testing.T) {
	got := (&HermesService{}).SupportedChannels()
	// iMessage rides on the BlueBubbles plugin baked into the Hermes
	// gateway; the plumbing is Hermes-side even though the bridge itself
	// runs on the operator's Mac, so it belongs in this list next to the
	// other three Hermes-native channels.
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
	// whatsapp is not supported on hermes — the capability gate returns before any
	// .env sync / gateway restart, so this is safe to call in a unit test.
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
