package picoclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/domain"
)

// telegramTargetsFile is the Device-owned store of known Telegram chats.
const telegramTargetsFile = "/root/.lumi/telegram_targets.json"

type telegramTargetEntry struct {
	ChatID string `json:"chat_id"`
	Type   string `json:"type"`
}

type telegramTargetsFileContent struct {
	Targets []telegramTargetEntry `json:"targets"`
}

// targetsFileMu serialises read-modify-write on telegramTargetsFile.
var targetsFileMu sync.Mutex

// GetTelegramBotToken returns the bot token from Device config.
func (s *PicoclawService) GetTelegramBotToken() string {
	return s.config.TelegramBotToken
}

// GetTelegramTargets reads the Device-owned target store.
func (s *PicoclawService) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	targetsFileMu.Lock()
	data, err := os.ReadFile(telegramTargetsFile)
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

func (s *PicoclawService) Broadcast(msg string, imagePath string) error {
	var sent int
	var lastErr error
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if err := ch.Send(msg, imagePath); err != nil {
			slog.Error("broadcast failed", "component", "picoclaw", "channel", ch.Name(), "err", err)
			lastErr = err
			continue
		}
		sent++
	}
	if sent == 0 && lastErr != nil {
		return lastErr
	}
	if sent == 0 {
		slog.Warn("broadcast: no channels configured", "component", "picoclaw")
	}
	return nil
}

func (s *PicoclawService) SendToUser(telegramID string, msg string, imagePath string) error {
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
	slog.Warn("sendToUser: no telegram channel configured", "component", "picoclaw")
	return nil
}

func (s *PicoclawService) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
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
	slog.Warn("sendToUserWithMedia: no telegram channel configured", "component", "picoclaw")
	return nil
}
