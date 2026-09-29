package domain

import "testing"

// stubGateway implements only SupportedChannels; other calls panic.
type stubGateway struct {
	AgentGateway
	supported []string
}

func (s stubGateway) SupportedChannels() []string { return s.supported }

func TestChannelSupported(t *testing.T) {
	gw := stubGateway{supported: []string{ChannelTelegram, ChannelSlack}}

	if !ChannelSupported(gw, ChannelSlack) {
		t.Errorf("ChannelSupported(slack) = false, want true")
	}
	if !ChannelSupported(gw, ChannelTelegram) {
		t.Errorf("ChannelSupported(telegram) = false, want true")
	}
	if ChannelSupported(gw, ChannelDiscord) {
		t.Errorf("ChannelSupported(discord) = true, want false")
	}
	if ChannelSupported(stubGateway{supported: nil}, ChannelTelegram) {
		t.Errorf("ChannelSupported on empty list = true, want false")
	}
}
