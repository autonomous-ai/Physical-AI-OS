package agent

import (
	"log/slog"

	migrateconfig "go.autonomous.ai/os/system/agent/migrate_config"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/server/config"
)

// ConfigMigration carries LLM provider config (API key + base URL) to the new runtime on a switch.
// Gate is config.json LLMConfigAppliedRuntime, advanced only after read, write and restart
// all succeed; it must run before EnsureOnboarding so provider patches see migrated values.
type ConfigMigration struct {
	cfg  *config.Config
	gw   domain.AgentGateway
	opts migrateconfig.Options
}

const cfgMigComponent = "cfg-migration"

// ProvideConfigMigration is the Wire provider for ConfigMigration.
func ProvideConfigMigration(cfg *config.Config, gw domain.AgentGateway) *ConfigMigration {
	return &ConfigMigration{
		cfg:  cfg,
		gw:   gw,
		opts: migrateconfig.DefaultOptions(cfg.OpenclawConfigDir, hermesHome),
	}
}

// Reconcile migrates LLM config if the runtime changed; failures retry next boot.
func (c *ConfigMigration) Reconcile() {
	current := c.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}

	if c.cfg.LLMConfigAppliedRuntime == current {
		slog.Debug("[cfg-migration] no switch detected, skip", "component", cfgMigComponent, "runtime", current)
		return
	}

	// Unset marker: record a baseline without migrating (avoids spurious upgrade-boot migration).
	if c.cfg.LLMConfigAppliedRuntime == "" {
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] baseline record failed", "component", cfgMigComponent, "runtime", current, "error", err)
		} else {
			slog.Info("[cfg-migration] baseline recorded (first boot, no migrate)", "component", cfgMigComponent, "runtime", current)
		}
		return
	}

	prev := c.cfg.LLMConfigAppliedRuntime
	from := migrateconfig.Runtime(prev)
	to := migrateconfig.Runtime(current)

	if !migrateconfig.CanMigrate(from) || !migrateconfig.CanMigrate(to) {
		slog.Info("[cfg-migration] no adapter for runtime pair, skip", "component", cfgMigComponent, "from", prev, "to", current)
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] advance marker failed", "component", cfgMigComponent, "error", err)
		}
		return
	}

	slog.Info("[cfg-migration] switch detected, starting migration", "component", cfgMigComponent, "from", prev, "to", current)

	migrated, err := migrateconfig.ReadConfig(from, c.opts)
	if err != nil {
		slog.Error("[cfg-migration] step1 read source failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	if migrated.Empty() {
		slog.Info("[cfg-migration] step1 source empty, nothing to carry", "component", cfgMigComponent, "from", prev, "to", current)
		if err := c.cfg.WithLockSave(func(cfg *config.Config) {
			cfg.LLMConfigAppliedRuntime = current
		}); err != nil {
			slog.Warn("[cfg-migration] advance marker failed", "component", cfgMigComponent, "error", err)
		}
		return
	}
	slog.Info("[cfg-migration] step1 read OK", "component", cfgMigComponent, "from", prev, "has_key", migrated.APIKey != "", "has_url", migrated.BaseURL != "")

	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		if migrated.APIKey != "" {
			cfg.LLMAPIKey = migrated.APIKey
		}
		if migrated.BaseURL != "" {
			// Re-normalize: claudecode stores the URL without /v1, other backends need it.
			cfg.LLMBaseURL = urlnorm.NormalizeBaseURL(migrated.BaseURL)
		}
	}); err != nil {
		slog.Warn("[cfg-migration] step2 config.json sync failed, will retry next boot", "component", cfgMigComponent, "error", err)
		return
	}
	slog.Info("[cfg-migration] step2 config.json synced", "component", cfgMigComponent)

	if err := migrateconfig.WriteConfig(to, migrated, c.opts); err != nil {
		slog.Warn("[cfg-migration] step3 write target failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	slog.Info("[cfg-migration] step3 write target OK", "component", cfgMigComponent, "to", current)

	if err := c.gw.RestartAgent(); err != nil {
		slog.Warn("[cfg-migration] step4 restart failed, will retry next boot", "component", cfgMigComponent, "from", prev, "to", current, "error", err)
		return
	}
	slog.Info("[cfg-migration] step4 gateway restarted", "component", cfgMigComponent, "to", current)

	// Advance the marker only after all steps succeed.
	if err := c.cfg.WithLockSave(func(cfg *config.Config) {
		cfg.LLMConfigAppliedRuntime = current
	}); err != nil {
		slog.Warn("[cfg-migration] step5 marker advance failed, will re-run next boot (idempotent)", "component", cfgMigComponent, "error", err)
		return
	}
	slog.Info("[cfg-migration] done", "component", cfgMigComponent, "from", prev, "to", current)
}
