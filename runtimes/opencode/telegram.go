package opencode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/domain"
)

// telegramTargetsFile is os-server's own list of Telegram chats to Broadcast
// proactive alerts (sensing/guard) to.
const telegramTargetsFile = "/root/.opencode/telegram_targets.json"

type telegramTargetEntry struct {
	ChatID string `json:"chat_id"`
	Type   string `json:"type"`
}

type telegramTargetsFileContent struct {
	Targets []telegramTargetEntry `json:"targets"`
}

// targetsFileMu serialises read-modify-write on telegramTargetsFile.
var targetsFileMu sync.Mutex

// telegramTargetsFilePath returns the targets store path (test override).
func (s *OpenCodeService) telegramTargetsFilePath() string {
	if s.telegramTargetsPath != "" {
		return s.telegramTargetsPath
	}
	return telegramTargetsFile
}

// upsertTelegramTarget records chatID in the targets store so outbound
// Broadcast reaches the chat the user wrote from.
// Called by the inbound poll loop (telegram_poll.go) on every accepted message; idempotent, atomic
// write.
func (s *OpenCodeService) upsertTelegramTarget(chatID, chatType string) {
	if chatID == "" {
		return
	}
	targetsFileMu.Lock()
	defer targetsFileMu.Unlock()
	path := s.telegramTargetsFilePath()
	var content telegramTargetsFileContent
	if data, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(data, &content)
	}
	for _, t := range content.Targets {
		if t.ChatID == chatID {
			return
		}
	}
	content.Targets = append(content.Targets, telegramTargetEntry{ChatID: chatID, Type: chatType})
	data, err := json.Marshal(content)
	if err != nil {
		slog.Warn("telegram targets marshal failed", "component", "opencode", "error", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		slog.Warn("telegram targets dir create failed", "component", "opencode", "error", err)
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		slog.Warn("telegram targets write failed", "component", "opencode", "error", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		slog.Warn("telegram targets rename failed", "component", "opencode", "error", err)
		return
	}
	slog.Info("telegram target upserted", "component", "opencode", "chatID", chatID, "type", chatType)
}

// GetTelegramBotToken returns the bot token from Device config.
func (s *OpenCodeService) GetTelegramBotToken() string {
	return s.config.TelegramBotToken
}

// GetTelegramTargets reads the target store (populated by the receive loop's
// upsertTelegramTarget, or operator-seeded).
func (s *OpenCodeService) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	targetsFileMu.Lock()
	data, err := os.ReadFile(s.telegramTargetsFilePath())
	targetsFileMu.Unlock()
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, fmt.Errorf("read telegram_targets.json: %w", err)
	}
	var content telegramTargetsFileContent
	if err := json.Unmarshal(data, &content); err != nil {
		return nil, fmt.Errorf("parse telegram_targets.json: %w", err)
	}
	out := make([]domain.TelegramTarget, 0, len(content.Targets))
	seen := make(map[string]bool, len(content.Targets))
	for _, t := range content.Targets {
		if t.ChatID == "" || seen[t.ChatID] {
			continue
		}
		seen[t.ChatID] = true
		chatType := t.Type
		if chatType == "" {
			if strings.HasPrefix(t.ChatID, "-") {
				chatType = "group"
			} else {
				chatType = "private"
			}
		}
		out = append(out, domain.TelegramTarget{ChatID: t.ChatID, Type: chatType})
	}
	return out, nil
}

func (s *OpenCodeService) Broadcast(msg string, imagePath string) error {
	var sent int
	var lastErr error
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if err := ch.Send(msg, imagePath); err != nil {
			slog.Error("broadcast failed", "component", "opencode", "channel", ch.Name(), "err", err)
			lastErr = err
			continue
		}
		sent++
	}
	if sent == 0 && lastErr != nil {
		return lastErr
	}
	if sent == 0 {
		slog.Warn("broadcast: no channels configured", "component", "opencode")
	}
	return nil
}

func (s *OpenCodeService) SendToUser(telegramID string, msg string, imagePath string) error {
	if telegramID == "" {
		return nil
	}
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if sender, ok := ch.(*TelegramSender); ok {
			return sender.SendToUser(telegramID, msg, imagePath)
		}
	}
	slog.Warn("sendToUser: no telegram channel configured", "component", "opencode")
	return nil
}

func (s *OpenCodeService) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
	if telegramID == "" {
		return nil
	}
	switch len(imagePaths) {
	case 0:
		return s.SendToUser(telegramID, msg, "")
	case 1:
		return s.SendToUser(telegramID, msg, imagePaths[0])
	}
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if sender, ok := ch.(*TelegramSender); ok {
			return sender.SendToUserWithMedia(telegramID, msg, imagePaths)
		}
	}
	slog.Warn("sendToUserWithMedia: no telegram channel configured", "component", "opencode")
	return nil
}
