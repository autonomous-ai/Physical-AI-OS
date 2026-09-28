package device

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// ErrSlackCredentialsMissing is returned when config.json has no credentials for
// the refreshed channel (MQTT status "slack_credentials_missing").
var ErrSlackCredentialsMissing = domain.ErrChannelCredentialsMissing

// ErrChannelNotSupported is returned when the active runtime cannot run the channel.
var ErrChannelNotSupported = domain.ErrChannelNotSupported

// AddChannel adds a messaging channel without full setup. Non-WhatsApp returns a
// nil channel; WhatsApp returns a pairing event stream the caller must drain.
func (s *Service) AddChannel(ctx context.Context, data domain.AddChannelRequest) (<-chan domain.PairingEvent, error) {
	channel := data.EffectiveChannel()

	// Reject before persisting so an unsupported channel leaves no dead token.
	if !domain.ChannelSupported(s.agentGateway, channel) {
		return nil, fmt.Errorf("%s on runtime %s: %w", channel, s.agentGateway.Name(), domain.ErrChannelNotSupported)
	}

	// Persist creds before the runtime apply, which may re-read config.json;
	// a failed apply is recovered by boot presync / ChannelReconcile.
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.Channel = channel
		switch channel {
		case domain.ChannelSlack:
			c.SlackBotToken = data.SlackBotToken
			c.SlackAppToken = data.SlackAppToken
			c.SlackUserID = data.SlackUserID
		case domain.ChannelDiscord:
			c.DiscordBotToken = data.DiscordBotToken
			c.DiscordGuildID = data.DiscordGuildID
			c.DiscordUserID = data.DiscordUserID
		case domain.ChannelWhatsapp:
			c.WhatsappUserID = data.WhatsappUserID
		case domain.ChannelIMessage:
			c.BluebubblesServerURL = data.BluebubblesServerURL
			c.BluebubblesPassword = data.BluebubblesPassword
			c.BluebubblesUserAddress = data.BluebubblesUserAddress
			c.BluebubblesCallerContext = data.BluebubblesCallerContext
		default:
			c.TelegramBotToken = data.TelegramBotToken
			c.TelegramUserID = data.TelegramUserID
		}
	}); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}

	if err := s.agentGateway.AddChannel(ctx, data); err != nil {
		return nil, fmt.Errorf("add channel in agent: %w", err)
	}
	slog.Info("added channel", "component", "device", "channel", channel)

	if channel != domain.ChannelWhatsapp {
		return nil, nil
	}
	// Existing session: no QR needed, emit a single success event.
	if s.agentGateway.HasWhatsappSession("default") {
		slog.Info("existing whatsapp session detected, skipping pairing", "component", "device")
		ch := make(chan domain.PairingEvent, 1)
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
		close(ch)
		return ch, nil
	}
	return s.agentGateway.PairWhatsapp(ctx), nil
}

// RefreshChannelConfig re-applies the channel config using credentials from
// config.json. Returns the runtime version ("Y.M.P", may be empty) or
// ErrSlackCredentialsMissing / ErrChannelNotSupported.
func (s *Service) RefreshChannelConfig(ctx context.Context, channel string) (string, error) {
	if !domain.ChannelSupported(s.agentGateway, channel) {
		return "", ErrChannelNotSupported
	}

	req := domain.RefreshChannelRequest{Channel: channel}
	switch channel {
	case domain.ChannelSlack:
		// Bot token is mandatory; AppToken is socket-mode-only.
		if s.config.SlackBotToken == "" {
			return "", ErrSlackCredentialsMissing
		}
		// HTTP mode: LLMAPIKey is the signing secret the backend proxy re-signs with.
		req.SlackBotToken = s.config.SlackBotToken
		req.SlackAppToken = s.config.SlackAppToken // ignored in http mode, kept for back-compat
		req.SlackUserID = s.config.SlackUserID
		req.SlackMode = "http"
		req.SlackSigningSecret = s.config.LLMAPIKey
	case domain.ChannelDiscord:
		if s.config.DiscordBotToken == "" {
			return "", ErrSlackCredentialsMissing
		}
		req.DiscordBotToken = s.config.DiscordBotToken
		req.DiscordGuildID = s.config.DiscordGuildID
		req.DiscordUserID = s.config.DiscordUserID
	case domain.ChannelTelegram:
		if s.config.TelegramBotToken == "" {
			return "", ErrSlackCredentialsMissing
		}
		req.TelegramBotToken = s.config.TelegramBotToken
		req.TelegramUserID = s.config.TelegramUserID
	case domain.ChannelIMessage:
		// All three are mandatory; the user address pins the operator's handle.
		if s.config.BluebubblesServerURL == "" || s.config.BluebubblesPassword == "" || s.config.BluebubblesUserAddress == "" {
			return "", ErrSlackCredentialsMissing
		}
		req.BluebubblesServerURL = s.config.BluebubblesServerURL
		req.BluebubblesPassword = s.config.BluebubblesPassword
		req.BluebubblesUserAddress = s.config.BluebubblesUserAddress
		req.BluebubblesCallerContext = s.config.BluebubblesCallerContext
	default:
		return "", ErrChannelNotSupported
	}
	return s.agentGateway.RefreshChannelConfig(ctx, req)
}

// SupportsChannel reports whether the active runtime can run the given channel.
func (s *Service) SupportsChannel(channel string) bool {
	return domain.ChannelSupported(s.agentGateway, channel)
}

// PairWhatsapp re-runs WhatsApp pairing without re-bootstrapping the channel config.
func (s *Service) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	return s.agentGateway.PairWhatsapp(ctx)
}

// StartClaudeLogin starts the claude.ai OAuth flow if the gateway supports it;
// otherwise it emits a single failure event.
func (s *Service) StartClaudeLogin(ctx context.Context) <-chan domain.PairingEvent {
	if p, ok := s.agentGateway.(domain.ClaudeLoginPairer); ok {
		return p.StartClaudeLogin(ctx)
	}
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "claude login not supported on " + s.agentGateway.Name() + " backend",
	}
	close(ch)
	return ch
}

// SubmitClaudeLoginCode feeds the browser authorization code to the login flow.
func (s *Service) SubmitClaudeLoginCode(code string) error {
	if p, ok := s.agentGateway.(domain.ClaudeLoginPairer); ok {
		return p.SubmitClaudeLoginCode(code)
	}
	return fmt.Errorf("claude login not supported on %s backend", s.agentGateway.Name())
}
