package openclaw

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/system/domain"
)

// RefreshChannelConfig rewrites channels.<channel> in openclaw.json using the canonical apply helpers, then restarts the gateway.
func (s *OpenclawService) RefreshChannelConfig(ctx context.Context, req domain.RefreshChannelRequest) (string, error) {
	_ = ctx
	runtime := currentOpenclawRuntime()
	runtimeStr := runtimeVersionString(runtime)

	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return runtimeStr, fmt.Errorf("read openclaw config: %w (device must be set up first)", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return runtimeStr, fmt.Errorf("parse openclaw config: %w", err)
	}

	channelsMap := ensureMap(configData, "channels")
	pluginsMap := ensureMap(configData, "plugins")
	entriesMap := ensureMap(pluginsMap, "entries")

	switch req.Channel {
	case domain.ChannelSlack:
		slackMap := ensureMap(channelsMap, domain.ChannelSlack)
		applySlackChannelConfig(slackMap, slackChannelConfig{
			BotToken:      req.SlackBotToken,
			AppToken:      req.SlackAppToken,
			UserID:        req.SlackUserID,
			Mode:          req.SlackMode,
			SigningSecret: req.SlackSigningSecret,
			WebhookPath:   req.SlackWebhookPath,
			Runtime:       runtime,
		})
		channelsMap[domain.ChannelSlack] = slackMap
		slackEntryMap := ensureMap(entriesMap, domain.ChannelSlack)
		slackEntryMap["enabled"] = true
	case domain.ChannelDiscord:
		discordMap := ensureMap(channelsMap, domain.ChannelDiscord)
		applyDiscordChannelConfig(discordMap, req.DiscordBotToken, req.DiscordUserID, req.DiscordGuildID)
		channelsMap[domain.ChannelDiscord] = discordMap
		ensureMap(entriesMap, domain.ChannelDiscord)["enabled"] = true
	case domain.ChannelTelegram:
		telegramMap := ensureMap(channelsMap, domain.ChannelTelegram)
		telegramMap["enabled"] = true
		telegramMap["botToken"] = req.TelegramBotToken
		if req.TelegramUserID != "" {
			telegramMap["dmPolicy"] = "allowlist"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], req.TelegramUserID)
		} else {
			telegramMap["dmPolicy"] = "open"
			telegramMap["allowFrom"] = mergeStringList(telegramMap["allowFrom"], "*")
		}
		channelsMap[domain.ChannelTelegram] = telegramMap
		ensureMap(entriesMap, domain.ChannelTelegram)["enabled"] = true
	default:
		return runtimeStr, fmt.Errorf("refresh not implemented for channel %q: %w", req.Channel, domain.ErrChannelNotSupported)
	}
	configData["channels"] = channelsMap
	configData["plugins"] = pluginsMap

	if toolsMap, ok := configData["tools"].(map[string]any); ok {
		if elevatedMap, ok := toolsMap["elevated"].(map[string]any); ok {
			elevatedAllowFrom := ensureMap(elevatedMap, "allowFrom")
			elevatedAllowFrom[req.Channel] = []any{"*"}
			elevatedMap["allowFrom"] = elevatedAllowFrom
		}
	}

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return runtimeStr, fmt.Errorf("marshal openclaw config: %w", err)
	}
	if existingPrimary := extractPrimaryModel(configData); existingPrimary != "" {
		setOSWriteFlag(s.config.OpenclawConfigDir, existingPrimary)
	}
	if err := os.WriteFile(configPath, written, 0600); err != nil {
		return runtimeStr, fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		return runtimeStr, fmt.Errorf("set openclaw config ownership: %w", err)
	}
	slog.Info("wrote openclaw config", "component", "openclaw", "path", configPath, "channel", req.Channel, "via", "refresh")

	if err := restartOpenclawGateway(); err != nil {
		return runtimeStr, err
	}
	slog.Info("gateway restarted", "component", "openclaw", "via", "refresh")
	return runtimeStr, nil
}

// runtimeVersionString returns "Y.M.P" when the runtime was detected, "" otherwise.
func runtimeVersionString(r RuntimeInfo) string {
	if !r.Detected {
		return ""
	}
	return fmt.Sprintf("%d.%d.%d", r.Year, r.Minor, r.Patch)
}
