package openclaw

// applyDiscordChannelConfig writes the canonical channels.discord block into discordMap.
func applyDiscordChannelConfig(discordMap map[string]any, botToken, userID, guildID string) {
	discordMap["enabled"] = true
	discordMap["dmPolicy"] = "allowlist"
	discordMap["token"] = botToken
	discordMap["allowFrom"] = mergeStringList(discordMap["allowFrom"], userID)
	if guildID != "" {
		discordMap["groupPolicy"] = "allowlist"
		discordMap["guilds"] = map[string]any{
			guildID: map[string]any{
				"requireMention": false,
				"users":          []string{userID},
			},
		}
	}
}
