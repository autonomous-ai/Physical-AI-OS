package openclaw

import "go.autonomous.ai/os/system/domain"

// SupportedChannels: telegram is built in; slack/discord/whatsapp are plugins installed on demand.
func (s *OpenclawService) SupportedChannels() []string {
	return []string{
		domain.ChannelTelegram,
		domain.ChannelSlack,
		domain.ChannelDiscord,
		domain.ChannelWhatsapp,
	}
}
