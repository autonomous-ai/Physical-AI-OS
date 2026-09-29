package openclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// GetTelegramBotToken returns the Telegram bot token from the agent runtime config.
func (s *OpenclawService) GetTelegramBotToken() string {
	if token := s.readOpenClawTelegramToken(); token != "" {
		return token
	}
	return s.config.TelegramBotToken
}

// GetTelegramTargets returns all Telegram chats by reading sessions.json directly from the OpenClaw agent's session store (no RPC round-trip required).
func (s *OpenclawService) GetTelegramTargets() ([]domain.TelegramTarget, error) {
	sessionsPath := filepath.Join(s.config.OpenclawConfigDir, "agents", "main", "sessions", "sessions.json")
	data, err := os.ReadFile(sessionsPath)
	if err != nil {
		return nil, fmt.Errorf("read sessions.json: %w", err)
	}

	type deliveryCtx struct {
		To string `json:"to,omitempty"`
	}
	type sessionEntry struct {
		DeliveryContext *deliveryCtx `json:"deliveryContext,omitempty"`
		LastTo          string       `json:"lastTo,omitempty"`
	}
	// sessions.json is a map[sessionID]sessionEntry
	var raw map[string]sessionEntry
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, fmt.Errorf("parse sessions.json: %w", err)
	}

	seen := make(map[string]bool)
	var targets []domain.TelegramTarget
	for sessionKey, sess := range raw {
		if !strings.Contains(sessionKey, ":telegram:") {
			continue
		}

		// Resolve chat ID from delivery target or session key.
		var chatID string
		if dc := sess.DeliveryContext; dc != nil && strings.HasPrefix(dc.To, "telegram:") {
			chatID = strings.TrimPrefix(dc.To, "telegram:")
		}
		if chatID == "" && strings.HasPrefix(sess.LastTo, "telegram:") {
			chatID = strings.TrimPrefix(sess.LastTo, "telegram:")
		}
		if chatID == "" {
			if i := strings.LastIndex(sessionKey, ":direct:"); i >= 0 {
				chatID = sessionKey[i+len(":direct:"):]
			} else if i := strings.LastIndex(sessionKey, ":group:"); i >= 0 {
				chatID = sessionKey[i+len(":group:"):]
			}
		}

		if chatID == "" || seen[chatID] {
			continue
		}
		seen[chatID] = true
		chatType := "private"
		if strings.HasPrefix(chatID, "-") {
			chatType = "group"
		}
		targets = append(targets, domain.TelegramTarget{ChatID: chatID, Type: chatType})
	}
	return targets, nil
}

// Broadcast sends a message to all connected messaging channels.
func (s *OpenclawService) Broadcast(msg string, imagePath string) error {
	var sent int
	var lastErr error
	for _, ch := range s.channels {
		if !ch.IsConfigured() {
			continue
		}
		if err := ch.Send(msg, imagePath); err != nil {
			slog.Error("broadcast failed", "component", "openclaw", "channel", ch.Name(), "err", err)
			lastErr = err
			continue
		}
		sent++
	}
	if sent == 0 && lastErr != nil {
		return lastErr
	}
	if sent == 0 {
		slog.Warn("broadcast: no channels configured", "component", "openclaw")
	}
	return nil
}

// SendToUser sends a direct message to a specific Telegram user ID.
func (s *OpenclawService) SendToUser(telegramID string, msg string, imagePath string) error {
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
	slog.Warn("sendToUser: no telegram channel configured", "component", "openclaw")
	return nil
}

// SendToUserWithMedia is the multi-image variant of SendToUser.
func (s *OpenclawService) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
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
	slog.Warn("sendToUserWithMedia: no telegram channel configured", "component", "openclaw")
	return nil
}

// readOpenClawTelegramToken reads the Telegram bot token from OpenClaw's config file.
func (s *OpenclawService) readOpenClawTelegramToken() string {
	candidates := []string{s.config.OpenclawConfigDir}
	home, _ := os.UserHomeDir()
	if home != "" {
		candidates = append(candidates, filepath.Join(home, ".openclaw"))
	}
	candidates = append(candidates, "/root/.openclaw", "/root/openclaw")

	var data []byte
	var err error
	for _, dir := range candidates {
		if dir == "" {
			continue
		}
		data, err = os.ReadFile(filepath.Join(dir, "openclaw.json"))
		if err == nil {
			break
		}
	}
	if err != nil {
		return ""
	}
	var cfg struct {
		Channels struct {
			Telegram struct {
				BotToken string `json:"botToken"`
			} `json:"telegram"`
		} `json:"channels"`
	}
	if json.Unmarshal(data, &cfg) != nil {
		return ""
	}
	return cfg.Channels.Telegram.BotToken
}
