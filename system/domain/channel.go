package domain

import "errors"

// ErrChannelNotSupported is returned when the active runtime cannot run the requested channel.
var ErrChannelNotSupported = errors.New("channel_not_supported")

// ErrChannelCredentialsMissing is returned when config.json has no credentials for the channel.
var ErrChannelCredentialsMissing = errors.New("channel_credentials_missing")

// ChannelSupported reports whether gw lists channel in its SupportedChannels().
func ChannelSupported(gw AgentGateway, channel string) bool {
	for _, c := range gw.SupportedChannels() {
		if c == channel {
			return true
		}
	}
	return false
}
