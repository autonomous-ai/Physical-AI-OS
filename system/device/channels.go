package device

import (
	"context"
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// ErrSlackCredentialsMissing is returned by RefreshChannelConfig when config.json
// has no credentials for the channel being refreshed — refresh cannot synthesize
// them, so the caller must run /api/device/setup or add_channel first. Aliased to
// the shared domain sentinel; the MQTT handler maps it to "slack_credentials_missing"
// (kept for wire back-compat).
var ErrSlackCredentialsMissing = domain.ErrChannelCredentialsMissing

// ErrChannelNotSupported is returned when the active runtime cannot run the
// requested channel. Aliased to the shared domain sentinel so runtime-level
// not-supported errors compare equal here and in the MQTT handlers.
var ErrChannelNotSupported = domain.ErrChannelNotSupported

// AddChannel adds a messaging channel to the agent without re-running full setup.
//
// For non-whatsapp channels the call is synchronous and the returned channel is
// nil — callers should publish a single success/failure response after this
// returns. For whatsapp the call returns a streaming event channel
// (pairing_starting → pairing_qr* → success | timeout | failure); the channel
// is closed when the flow terminates. Callers MUST drain. `success` is emitted
// both for first-time pairing and for resumed sessions (creds already on
// disk).
func (s *Service) AddChannel(ctx context.Context, data domain.AddChannelRequest) (<-chan domain.PairingEvent, error) {
	channel := data.EffectiveChannel()

	// 1. Capability gate — reject before persisting anything, so an unsupported
	// channel never leaves a dead token in config.json masquerading as configured.
	if !domain.ChannelSupported(s.agentGateway, channel) {
		return nil, fmt.Errorf("%s on runtime %s: %w", channel, s.agentGateway.Name(), domain.ErrChannelNotSupported)
	}

	// 2. Persist creds FIRST: a config-reading runtime apply (hermes presync re-reads
	// config.json to rebuild ~/.hermes/.env) must see the new tokens. A transient
	// apply failure then leaves creds persisted, which is the recoverable direction —
	// the boot presync / ChannelReconcile re-applies them. Mutate INSIDE WithLockSave so
	// the field writes + marshal are atomic against concurrent config writers
	// (UpdateConfig, the primary-model watcher).
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
			// Optional caller-context prompt — plain field, no validation
			// (empty means "use plugin default"). See config.go for the flow.
			c.BluebubblesCallerContext = data.BluebubblesCallerContext
		default:
			c.TelegramBotToken = data.TelegramBotToken
			c.TelegramUserID = data.TelegramUserID
		}
	}); err != nil {
		slog.Error("save config failed", "component", "device", "error", err)
	}

	// 3. Apply the channel in the active runtime.
	if err := s.agentGateway.AddChannel(ctx, data); err != nil {
		return nil, fmt.Errorf("add channel in agent: %w", err)
	}
	slog.Info("added channel", "component", "device", "channel", channel)

	if channel != domain.ChannelWhatsapp {
		return nil, nil
	}
	// Existing Baileys creds on disk → no QR needed; emit a single success
	// event so the caller's drain loop sees the same terminal status it would
	// for a first-time pair.
	if s.agentGateway.HasWhatsappSession("default") {
		slog.Info("existing whatsapp session detected, skipping pairing", "component", "device")
		ch := make(chan domain.PairingEvent, 1)
		ch <- domain.PairingEvent{Status: domain.PairingStatusSuccess}
		close(ch)
		return ch, nil
	}
	return s.agentGateway.PairWhatsapp(ctx), nil
}

// RefreshChannelConfig re-applies the canonical channel config block to
// openclaw.json on the device. Triggered by the channel.refresh_config MQTT kind
// to fix older devices whose config predates schema additions (e.g. the
// socketMode block, object-form streaming, dmPolicy).
//
// Reads credentials from config.json (set previously by /api/device/setup or
// add_channel) — refresh does NOT carry tokens over MQTT. Delegates the
// write+restart to AgentGateway.RefreshChannelConfig, the separate
// non-AddChannel code path so the two flows can diverge cleanly.
//
// Returns the detected runtime version string ("Y.M.P", empty when undetected)
// and sentinel errors the MQTT handler maps to stable status codes:
//   - ErrSlackCredentialsMissing — config.json has no credentials for the channel
//   - ErrChannelNotSupported     — the active runtime can't run this channel
func (s *Service) RefreshChannelConfig(ctx context.Context, channel string) (string, error) {
	// Capability gate first: a channel the active runtime can't run is "not
	// supported" regardless of whether creds happen to be on disk.
	if !domain.ChannelSupported(s.agentGateway, channel) {
		return "", ErrChannelNotSupported
	}

	req := domain.RefreshChannelRequest{Channel: channel}
	switch channel {
	case domain.ChannelSlack:
		// Bot token is the one mandatory credential for both transports.
		// AppToken is socket-mode-only — refresh succeeds without it when
		// migrating to HTTP mode (signing_secret comes from LLMAPIKey instead,
		// which the device always has).
		if s.config.SlackBotToken == "" {
			return "", ErrSlackCredentialsMissing
		}
		// Refresh defaults to HTTP mode: use the device's llm_api_key (LLMAPIKey
		// on disk) as the signingSecret so it matches what the backend proxy
		// re-signs with. Socket-mode installs flip to HTTP the first time the
		// backend sends channel.refresh_config — no per-device add_channel push.
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
		// All three fields are mandatory: the server URL is where the plugin
		// dials, the password authenticates every REST call, and the allowed
		// user address is the filter that pins Intern to the operator's own
		// iMessage handle (bridge default is deny-all).
		if s.config.BluebubblesServerURL == "" || s.config.BluebubblesPassword == "" || s.config.BluebubblesUserAddress == "" {
			return "", ErrSlackCredentialsMissing
		}
		req.BluebubblesServerURL = s.config.BluebubblesServerURL
		req.BluebubblesPassword = s.config.BluebubblesPassword
		req.BluebubblesUserAddress = s.config.BluebubblesUserAddress
		// Caller-context prompt (optional, empty is fine).
		req.BluebubblesCallerContext = s.config.BluebubblesCallerContext
	default:
		return "", ErrChannelNotSupported
	}
	return s.agentGateway.RefreshChannelConfig(ctx, req)
}

// SupportsChannel reports whether the active runtime can run the given channel.
// Used by the HTTP add-channel handler to reject an unsupported channel
// synchronously (before its fire-and-forget goroutine) with a real error.
func (s *Service) SupportsChannel(channel string) bool {
	return domain.ChannelSupported(s.agentGateway, channel)
}

// PairWhatsapp re-runs the WhatsApp Linked Devices pairing flow without
// re-bootstrapping the channel config. Used by the whatsapp_pair MQTT command
// for re-pair after session loss.
func (s *Service) PairWhatsapp(ctx context.Context) <-chan domain.PairingEvent {
	return s.agentGateway.PairWhatsapp(ctx)
}

// StartClaudeLogin starts the claude.ai OAuth login flow when the active
// gateway supports it (claudecode — domain.ClaudeLoginPairer is an optional
// interface, like SlackBridge). Other runtimes get a one-shot failure event so
// the MQTT drain loop exits cleanly. Used by the claudecode_login MQTT command.
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

// SubmitClaudeLoginCode feeds the browser authorization code back into the
// waiting login flow. Used by the claudecode_login_code MQTT command.
func (s *Service) SubmitClaudeLoginCode(code string) error {
	if p, ok := s.agentGateway.(domain.ClaudeLoginPairer); ok {
		return p.SubmitClaudeLoginCode(code)
	}
	return fmt.Errorf("claude login not supported on %s backend", s.agentGateway.Name())
}
