package claudecode

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/system/domain"
)

func (s *ClaudeCodeService) HasWhatsappSession(_ string) bool { return false }

// PairWhatsapp — WhatsApp pairing requires a Baileys-style plugin which lives
// only in OpenClaw.
func (s *ClaudeCodeService) PairWhatsapp(_ context.Context) <-chan domain.PairingEvent {
	ch := make(chan domain.PairingEvent, 1)
	ch <- domain.PairingEvent{
		Status: domain.PairingStatusFailure,
		Error:  "whatsapp pairing not supported on claudecode backend",
	}
	close(ch)
	return ch
}

// RestartAgent restarts the claudecode bridge via systemctl so callers that need
// a full reload (workspace / .env / .mcp.json re-read — all loaded at Claude
// session start) get it.
func (s *ClaudeCodeService) RestartAgent() error {
	slog.Info("RestartAgent: restarting claudecode bridge", "component", "claudecode")
	return restartClaudeCodeGateway()
}

// RefreshModelsConfig — the model is selected via ANTHROPIC_MODEL in
// /root/.claudecode/.env, which presync owns (synced from config.json
// llm_model).
func (s *ClaudeCodeService) RefreshModelsConfig() error {
	return domain.ErrNotSupportedByRuntime
}

// FetchChatHistory — Claude Code keeps its transcript as internal session JSONL
// under ~/.claude/projects; there is no stable read API for it.
func (s *ClaudeCodeService) FetchChatHistory(_ string, _ int) (json.RawMessage, error) {
	return nil, nil
}

// GetConfigJSON returns the workspace project settings
// (workspace/.claude/settings.json) — the only JSON config the backend owns that
// is safe to expose (no secrets; ANTHROPIC_* keys live in .env, never returned).
func (s *ClaudeCodeService) GetConfigJSON() (json.RawMessage, error) {
	path := filepath.Join(claudecodeWorkspaceDir, ".claude", "settings.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return json.RawMessage("{}"), nil
		}
		return nil, err
	}
	return json.RawMessage(data), nil
}

// StartModelSync — the model registry is fixed (ANTHROPIC_MODEL env, presync-
// owned).
func (s *ClaudeCodeService) StartModelSync(ctx context.Context) {
	<-ctx.Done()
}

// UpdatePrimaryModel — the model is pinned in .env (ANTHROPIC_MODEL) by
// presync (from config.json llm_model), not patched per-call.
func (s *ClaudeCodeService) UpdatePrimaryModel(_ string) error {
	return domain.ErrNotSupportedByRuntime
}

// StartPrimaryModelWatch — no openclaw.json-style agent config file to watch.
func (s *ClaudeCodeService) StartPrimaryModelWatch(ctx context.Context) {
	<-ctx.Done()
}

// GetConfiguredChannel — device config is the source of truth.
func (s *ClaudeCodeService) GetConfiguredChannel() string {
	if s.config.TelegramBotToken != "" {
		return "telegram"
	}
	if s.config.DiscordBotToken != "" {
		return "discord"
	}
	return "channel"
}
