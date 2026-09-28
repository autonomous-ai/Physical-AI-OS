package agent

import (
	"log/slog"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/server/config"
)

// UserProfileReconcile retires people from every runtime's USER.md once they no
// longer have a face/voice enrollment (see migratepersona.ReconcileUserProfiles).
type UserProfileReconcile struct {
	opts    migratepersona.Options
	execute bool
}

// ProvideUserProfileReconcile builds the reconciler; config can set observe-only (log, no writes).
func ProvideUserProfileReconcile(cfg *config.Config) *UserProfileReconcile {
	opts := migratepersona.DefaultOptions(cfg.OpenclawConfigDir, hermesHome)
	return &UserProfileReconcile{
		opts:    opts,
		execute: cfg.UserProfileReconcileEnabled(),
	}
}

// Reconcile runs one pass; errors are logged, never fatal.
func (r *UserProfileReconcile) Reconcile() {
	actions, err := migratepersona.ReconcileUserProfiles(r.opts, r.execute)
	if err != nil {
		slog.Warn("user profile reconcile failed; profiles left untouched",
			"component", "user-reconcile", "error", err)
		return
	}
	if len(actions) == 0 {
		return
	}
	mode := "dry-run (agent.user_profile_reconcile=false)"
	if r.execute {
		mode = "applied"
	}
	for _, a := range actions {
		slog.Info("user profile reconcile", "component", "user-reconcile",
			"mode", mode, "path", a.Path, "kind", a.Kind,
			"detail", a.Detail, "reason", a.Reason)
	}
}
