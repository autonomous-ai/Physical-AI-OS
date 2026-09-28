package http

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/serializers"
)

// ResetMemory handles POST /api/agent/memory/reset (#421): backs up and clears workspace memory
// files, then re-runs onboarding. The in-flight session is untouched.
func (h *AgentHandler) ResetMemory(c *gin.Context) {
	opts := migratepersona.DefaultOptions(h.config.OpenclawConfigDir, "")
	rep, err := migratepersona.ResetMemoryFiles(opts)
	if err != nil {
		// rep carries partial progress so the operator can find the backups.
		slog.Error("memory reset failed", "component", "memory-guard", "error", err, "cleared", rep.Cleared, "backup_dirs", rep.BackupDirs)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("memory reset: "+err.Error()))
		return
	}
	// Counts and paths only — never memory text — in the flow event.
	flow.Log("memory_reset", map[string]any{"cleared": len(rep.Cleared), "backup_dirs": rep.BackupDirs})
	slog.Warn("agent memory reset", "component", "memory-guard", "cleared", len(rep.Cleared), "backups", rep.BackupDirs)
	if err := h.agentGateway.EnsureOnboarding(); err != nil {
		// Not fatal: KNOWLEDGE.md is re-seeded on the next boot.
		slog.Warn("onboarding re-seed after memory reset failed", "component", "memory-guard", "error", err)
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(rep))
}
