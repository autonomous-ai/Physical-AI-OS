package device

import (
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestSetupIMessageChannelPersistsCredentials(t *testing.T) {
	s := &Service{config: &config.Config{TelegramBotToken: "saved-telegram"}}
	data := domain.SetupRequest{Channel: domain.ChannelIMessage, BluebubblesServerURL: "https://messages.example", BluebubblesPassword: "secret", BluebubblesUserAddress: "owner@example.com", TelegramBotToken: "unrelated-token"}
	s.applySetupChannel(data)
	if s.config.Channel != domain.ChannelIMessage || s.config.BluebubblesServerURL != data.BluebubblesServerURL || s.config.BluebubblesPassword != data.BluebubblesPassword || s.config.BluebubblesUserAddress != data.BluebubblesUserAddress {
		t.Fatal("full setup did not persist iMessage channel credentials")
	}
	if s.config.TelegramBotToken != "saved-telegram" {
		t.Fatal("iMessage setup changed Telegram credentials")
	}
}

func TestWifiProvisionIMessageChannelPersistsAndForwardsCredentials(t *testing.T) {
	s := &Service{config: &config.Config{Channel: domain.ChannelTelegram, TelegramBotToken: "saved-telegram", LLMAPIKey: "llm-key", LLMBaseURL: "https://llm.example", LLMModel: "model", DeviceID: "device"}}
	s.applyWifiProvisionChannel(domain.WifiProvisionRequest{Channel: domain.ChannelIMessage, BluebubblesServerURL: " https://messages.example ", BluebubblesPassword: " secret ", BluebubblesUserAddress: " owner@example.com ", TelegramBotToken: "unrelated-token"})
	got := s.wifiProvisionSetupRequest()
	if got.Channel != domain.ChannelIMessage || got.BluebubblesServerURL != "https://messages.example" || got.BluebubblesPassword != "secret" || got.BluebubblesUserAddress != "owner@example.com" {
		t.Fatal("Wi-Fi provisioning did not forward persisted iMessage credentials")
	}
	if got.LLMAPIKey != "llm-key" || got.LLMBaseURL != "https://llm.example" || got.LLMModel != "model" || got.DeviceID != "device" || got.TelegramBotToken != "saved-telegram" {
		t.Fatal("Wi-Fi provisioning lost existing runtime settings")
	}
	// Reconnecting Wi-Fi without channel fields must retain the working channel.
	s.applyWifiProvisionChannel(domain.WifiProvisionRequest{BluebubblesPassword: "   "})
	if s.wifiProvisionSetupRequest() != got {
		t.Fatal("omitted channel credentials changed persisted settings")
	}
	// A partial edit also works when the current channel is retained.
	s.applyWifiProvisionChannel(domain.WifiProvisionRequest{BluebubblesUserAddress: "new@example.com"})
	updated := s.wifiProvisionSetupRequest()
	if updated.BluebubblesUserAddress != "new@example.com" || updated.BluebubblesPassword != got.BluebubblesPassword || updated.BluebubblesServerURL != got.BluebubblesServerURL {
		t.Fatal("partial iMessage update lost existing credentials")
	}
}

func TestWifiProvisionIgnoresIMessageCredentialsForOtherChannels(t *testing.T) {
	for _, channel := range []string{domain.ChannelTelegram, domain.ChannelSlack, domain.ChannelDiscord} {
		t.Run(channel, func(t *testing.T) {
			s := &Service{config: &config.Config{Channel: domain.ChannelIMessage, BluebubblesServerURL: "saved-url", BluebubblesPassword: "saved-password", BluebubblesUserAddress: "saved-address"}}
			s.applyWifiProvisionChannel(domain.WifiProvisionRequest{Channel: channel, BluebubblesServerURL: "unrelated-url", BluebubblesPassword: "unrelated-password", BluebubblesUserAddress: "unrelated-address"})
			if s.config.Channel != channel || s.config.BluebubblesServerURL != "saved-url" || s.config.BluebubblesPassword != "saved-password" || s.config.BluebubblesUserAddress != "saved-address" {
				t.Fatal("other channel update changed iMessage credentials")
			}
		})
	}
}
