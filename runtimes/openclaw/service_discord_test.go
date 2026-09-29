package openclaw

import "testing"

func TestApplyDiscordChannelConfig_WithGuild(t *testing.T) {
	m := map[string]any{}
	applyDiscordChannelConfig(m, "discord-bot-token", "U999", "G123")

	if m["enabled"] != true {
		t.Errorf("enabled = %v, want true", m["enabled"])
	}
	if m["dmPolicy"] != "allowlist" {
		t.Errorf("dmPolicy = %v, want allowlist", m["dmPolicy"])
	}
	if m["token"] != "discord-bot-token" {
		t.Errorf("token = %v, want discord-bot-token", m["token"])
	}
	if got, ok := m["allowFrom"].([]string); !ok || len(got) != 1 || got[0] != "U999" {
		t.Errorf("allowFrom = %v, want [U999]", m["allowFrom"])
	}
	if m["groupPolicy"] != "allowlist" {
		t.Errorf("groupPolicy = %v, want allowlist", m["groupPolicy"])
	}
	guilds, ok := m["guilds"].(map[string]any)
	if !ok {
		t.Fatalf("guilds missing/wrong type: %v", m["guilds"])
	}
	g, ok := guilds["G123"].(map[string]any)
	if !ok {
		t.Fatalf("guild G123 missing: %v", guilds)
	}
	if g["requireMention"] != false {
		t.Errorf("requireMention = %v, want false", g["requireMention"])
	}
	if users, ok := g["users"].([]string); !ok || len(users) != 1 || users[0] != "U999" {
		t.Errorf("guild users = %v, want [U999]", g["users"])
	}
}

func TestApplyDiscordChannelConfig_NoGuild(t *testing.T) {
	m := map[string]any{}
	applyDiscordChannelConfig(m, "discord-bot-token", "U999", "")

	if _, ok := m["groupPolicy"]; ok {
		t.Errorf("groupPolicy must not be set without a guild id")
	}
	if _, ok := m["guilds"]; ok {
		t.Errorf("guilds must not be set without a guild id")
	}
	if m["dmPolicy"] != "allowlist" {
		t.Errorf("dmPolicy = %v, want allowlist", m["dmPolicy"])
	}
}
