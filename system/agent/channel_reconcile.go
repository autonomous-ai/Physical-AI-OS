package agent

import (
	"context"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// channelReapplyTimeout caps one channel re-apply so a stuck gateway restart can't hang startup.
const channelReapplyTimeout = 5 * time.Minute

// ChannelReconcile re-applies configured messaging channels after any runtime switch
// and records channels the runtime cannot run. Gated by config.ChannelsAppliedRuntime.
type ChannelReconcile struct {
	cfg *config.Config
	gw  domain.AgentGateway
}

// ProvideChannelReconcile is the Wire provider for ChannelReconcile.
func ProvideChannelReconcile(cfg *config.Config, gw domain.AgentGateway) *ChannelReconcile {
	return &ChannelReconcile{cfg: cfg, gw: gw}
}

// configuredChannels returns one AddChannelRequest per channel with credentials in config.json.
func (r *ChannelReconcile) configuredChannels() []domain.AddChannelRequest {
	c := r.cfg
	var out []domain.AddChannelRequest
	if c.TelegramBotToken != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:          domain.ChannelTelegram,
			TelegramBotToken: c.TelegramBotToken,
			TelegramUserID:   c.TelegramUserID,
		})
	}
	if c.SlackBotToken != "" {
		// Fleet uses HTTP mode (default would be Socket Mode); signing secret = llm_api_key.
		out = append(out, domain.AddChannelRequest{
			Channel:            domain.ChannelSlack,
			SlackBotToken:      c.SlackBotToken,
			SlackAppToken:      c.SlackAppToken,
			SlackUserID:        c.SlackUserID,
			SlackMode:          "http",
			SlackSigningSecret: c.LLMAPIKey,
		})
	}
	if c.DiscordBotToken != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:         domain.ChannelDiscord,
			DiscordBotToken: c.DiscordBotToken,
			DiscordGuildID:  c.DiscordGuildID,
			DiscordUserID:   c.DiscordUserID,
		})
	}
	if c.WhatsappUserID != "" {
		out = append(out, domain.AddChannelRequest{
			Channel:        domain.ChannelWhatsapp,
			WhatsappUserID: c.WhatsappUserID,
		})
	}
	return out
}

// Reconcile re-applies channels if the runtime changed; failures leave the marker for next-boot retry.
func (r *ChannelReconcile) Reconcile() {
	current := r.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}
	if r.cfg.ChannelsAppliedRuntime == current {
		return
	}

	// Unset marker: record a baseline only, to avoid a gratuitous restart on upgrade boot.
	if r.cfg.ChannelsAppliedRuntime == "" {
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.ChannelsAppliedRuntime = current }); err != nil {
			slog.Warn("channel reconcile: record baseline failed", "component", "agent", "error", err)
			return
		}
		slog.Info("channel reconcile: baseline recorded (no re-apply)", "component", "agent", "runtime", current)
		return
	}

	slog.Info("channel reconcile: runtime changed, re-applying channels",
		"component", "agent", "from", r.cfg.ChannelsAppliedRuntime, "to", current)

	var unsupported []string
	applyErr := false
	for _, req := range r.configuredChannels() {
		if !domain.ChannelSupported(r.gw, req.Channel) {
			slog.Warn("channel not supported on runtime — leaving creds for switch-back",
				"component", "agent", "channel", req.Channel, "runtime", current)
			unsupported = append(unsupported, req.Channel)
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), channelReapplyTimeout)
		err := r.gw.AddChannel(ctx, req)
		cancel()
		if err != nil {
			slog.Error("channel re-apply failed; will retry next boot",
				"component", "agent", "channel", req.Channel, "error", err)
			applyErr = true
			continue
		}
		slog.Info("channel re-applied to new runtime",
			"component", "agent", "channel", req.Channel, "runtime", current)
	}

	// Persist only on a clean pass: after an apply error `unsupported` may be incomplete.
	if applyErr {
		slog.Warn("channel reconcile: apply error — leaving marker + unsupported list for next-boot retry",
			"component", "agent", "runtime", current)
		return
	}
	if err := r.cfg.WithLockSave(func(c *config.Config) {
		c.ChannelsUnsupported = unsupported
		c.ChannelsAppliedRuntime = current
	}); err != nil {
		slog.Warn("channel reconcile: persist marker failed", "component", "agent", "error", err)
	}
}
