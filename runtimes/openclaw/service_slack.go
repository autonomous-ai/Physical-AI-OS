package openclaw

// slackChannelConfig holds the inputs for applySlackChannelConfig.
type slackChannelConfig struct {
	BotToken string
	AppToken string // socket mode only
	// UserID is collected for parity with the telegram/discord configs but is NOT used to gate Slack DMs — dmPolicy is always "open" (see applySlackChannelConfig).
	UserID string

	// Mode is "" / "socket" (outbound WSS, needs AppToken) or "http" (proxy-forwarded, needs SigningSecret).
	Mode          string
	SigningSecret string // http mode only
	WebhookPath   string // http mode only; defaults to /slack/events

	// Runtime is the installed openclaw version, used to pick field shapes the runtime accepts (see streaming/socketMode below).
	Runtime RuntimeInfo
}

// applySlackChannelConfig writes the canonical channels.slack block into slackMap.
func applySlackChannelConfig(slackMap map[string]any, cfg slackChannelConfig) {
	slackMap["enabled"] = true
	slackMap["botToken"] = cfg.BotToken
	slackMap["dm"] = map[string]any{
		"enabled":      true,
		"groupEnabled": false,
	}
	slackMap["groupPolicy"] = "open"
	slackMap["ackReaction"] = "eyes"
	slackMap["typingReaction"] = "writing_hand"
	if cfg.Runtime.AtLeast(2026, 4) {
		slackMap["streaming"] = map[string]any{
			"mode":            "partial",
			"nativeTransport": true,
		}
	} else {
		slackMap["streaming"] = "partial"
	}
	slackMap["userTokenReadOnly"] = true
	slackMap["slashCommand"] = map[string]any{
		"enabled":       true,
		"name":          "openclaw",
		"sessionPrefix": "slack:slash",
		"ephemeral":     true,
	}
	slackMap["commands"] = map[string]any{
		"native": true,
	}
	slackMap["dmPolicy"] = "open"
	slackMap["allowFrom"] = mergeStringList(slackMap["allowFrom"], "*")

	if cfg.Mode == "http" {
		slackMap["mode"] = "http"
		slackMap["signingSecret"] = cfg.SigningSecret
		webhookPath := cfg.WebhookPath
		if webhookPath == "" {
			webhookPath = "/slack/events"
		}
		slackMap["webhookPath"] = webhookPath
		delete(slackMap, "appToken")
		delete(slackMap, "socketMode")
	} else {
		slackMap["mode"] = "socket"
		slackMap["appToken"] = cfg.AppToken
		if cfg.Runtime.AtLeast(2026, 4) {
			socketModeMap := ensureMap(slackMap, "socketMode")
			setDefaultValue(socketModeMap, "clientPingTimeout", 20000)
			setDefaultValue(socketModeMap, "serverPingTimeout", 30000)
			slackMap["socketMode"] = socketModeMap
		}
		delete(slackMap, "signingSecret")
		delete(slackMap, "webhookPath")
	}

	delete(slackMap, "requireMention")
	delete(slackMap, "nativeStreaming")
}
