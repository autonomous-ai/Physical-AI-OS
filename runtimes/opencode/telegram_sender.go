package opencode

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/flow"
)

// telegramMaxMediaGroup is the upper bound imposed by Telegram's sendMediaGroup
// endpoint.
const telegramMaxMediaGroup = 10

// TelegramSender delivers OUTBOUND messages via the Telegram Bot API (identical
// wire protocol to the openclaw / hermes implementations).
type TelegramSender struct {
	svc *OpenCodeService
}

func (t *TelegramSender) Name() string { return "telegram" }

func (t *TelegramSender) IsConfigured() bool {
	return t.svc.GetTelegramBotToken() != ""
}

func (t *TelegramSender) Send(msg string, imagePath string) error {
	botToken := t.svc.GetTelegramBotToken()
	targets, err := t.svc.GetTelegramTargets()
	if err != nil {
		return fmt.Errorf("get telegram targets: %w", err)
	}
	if len(targets) == 0 {
		slog.Warn("telegram: no chats found", "component", "opencode")
		return nil
	}

	slog.Info("telegram broadcast", "component", "opencode", "chats", len(targets), "hasImage", imagePath != "")

	var photoBytes []byte
	if imagePath != "" {
		if data, err := os.ReadFile(imagePath); err == nil {
			photoBytes = data
		} else {
			slog.Warn("telegram: failed to read image", "component", "opencode", "path", imagePath, "err", err)
		}
	}

	client := &http.Client{Timeout: 10 * time.Second}
	for _, tgt := range targets {
		if photoBytes != nil {
			sendTelegramPhoto(client, botToken, tgt.ChatID, msg, photoBytes)
		} else {
			sendTelegramMessage(client, botToken, tgt.ChatID, msg)
		}
	}

	flow.Log("telegram_alert_broadcast", map[string]any{
		"method":  "bot_api",
		"chats":   len(targets),
		"message": msg,
	})

	return nil
}

func (t *TelegramSender) SendToUser(telegramID string, msg string, imagePath string) error {
	if telegramID == "" {
		return nil
	}
	botToken := t.svc.GetTelegramBotToken()
	if botToken == "" {
		return fmt.Errorf("telegram bot token not configured")
	}

	slog.Info("telegram dm", "component", "opencode", "telegram_id", telegramID, "hasImage", imagePath != "")

	var photoBytes []byte
	if imagePath != "" {
		if data, err := os.ReadFile(imagePath); err == nil {
			photoBytes = data
		} else {
			slog.Warn("telegram: failed to read image", "component", "opencode", "path", imagePath, "err", err)
		}
	}

	client := &http.Client{Timeout: 10 * time.Second}
	if photoBytes != nil {
		sendTelegramPhoto(client, botToken, telegramID, msg, photoBytes)
	} else {
		sendTelegramMessage(client, botToken, telegramID, msg)
	}

	flow.Log("telegram_dm", map[string]any{
		"method":      "bot_api",
		"telegram_id": telegramID,
		"message":     msg,
	})
	return nil
}

func (t *TelegramSender) SendToUserWithMedia(telegramID string, msg string, imagePaths []string) error {
	if telegramID == "" {
		return nil
	}
	botToken := t.svc.GetTelegramBotToken()
	if botToken == "" {
		return fmt.Errorf("telegram bot token not configured")
	}

	if len(imagePaths) > telegramMaxMediaGroup {
		imagePaths = imagePaths[:telegramMaxMediaGroup]
	}

	type photo struct {
		name string
		data []byte
	}
	photos := make([]photo, 0, len(imagePaths))
	for i, p := range imagePaths {
		data, err := os.ReadFile(p)
		if err != nil {
			slog.Warn("telegram: media group skip", "component", "opencode", "path", p, "err", err)
			continue
		}
		photos = append(photos, photo{name: fmt.Sprintf("photo%d", i), data: data})
	}
	switch len(photos) {
	case 0:
		slog.Warn("telegram: no readable media, falling back to text", "component", "opencode", "telegram_id", telegramID)
		client := &http.Client{Timeout: 10 * time.Second}
		sendTelegramMessage(client, botToken, telegramID, msg)
		return nil
	case 1:
		client := &http.Client{Timeout: 10 * time.Second}
		sendTelegramPhoto(client, botToken, telegramID, msg, photos[0].data)
		return nil
	}

	slog.Info("telegram media group", "component", "opencode", "telegram_id", telegramID, "count", len(photos))

	media := make([]map[string]string, 0, len(photos))
	for i, p := range photos {
		entry := map[string]string{
			"type":  "photo",
			"media": "attach://" + p.name,
		}
		if i == 0 {
			entry["caption"] = msg
		}
		media = append(media, entry)
	}
	mediaJSON, err := json.Marshal(media)
	if err != nil {
		return fmt.Errorf("marshal media: %w", err)
	}

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	w.WriteField("chat_id", telegramID)
	w.WriteField("media", string(mediaJSON))
	for _, p := range photos {
		part, ferr := w.CreateFormFile(p.name, p.name+".jpg")
		if ferr != nil {
			return fmt.Errorf("create part: %w", ferr)
		}
		part.Write(p.data)
	}
	w.Close()

	apiURL := fmt.Sprintf("https://api.telegram.org/bot%s/sendMediaGroup", botToken)
	client := &http.Client{Timeout: 15 * time.Second}
	resp, err := client.Post(apiURL, w.FormDataContentType(), &buf)
	if err != nil {
		slog.Error("telegram sendMediaGroup failed", "component", "opencode", "chatID", telegramID, "err", err)
		return err
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		slog.Error("telegram sendMediaGroup error", "component", "opencode", "chatID", telegramID, "status", resp.StatusCode, "body", string(body))
		return fmt.Errorf("sendMediaGroup status %d", resp.StatusCode)
	}
	slog.Info("telegram sendMediaGroup sent", "component", "opencode", "chatID", telegramID, "photos", len(photos))

	flow.Log("telegram_dm", map[string]any{
		"method":      "bot_api_media_group",
		"telegram_id": telegramID,
		"photos":      len(photos),
		"message":     msg,
	})
	return nil
}

func sendTelegramMessage(client *http.Client, token, chatID, text string) {
	apiURL := fmt.Sprintf("https://api.telegram.org/bot%s/sendMessage", token)
	payload := fmt.Sprintf(`{"chat_id":%q,"text":%q}`, chatID, text)
	resp, err := client.Post(apiURL, "application/json", strings.NewReader(payload))
	if err != nil {
		slog.Error("telegram sendMessage failed", "component", "opencode", "chatID", chatID, "err", err)
		return
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		slog.Error("telegram sendMessage error", "component", "opencode", "chatID", chatID, "status", resp.StatusCode, "body", string(body))
		return
	}
	slog.Info("telegram sendMessage sent", "component", "opencode", "chatID", chatID)
}

func sendTelegramPhoto(client *http.Client, token, chatID, caption string, photo []byte) {
	apiURL := fmt.Sprintf("https://api.telegram.org/bot%s/sendPhoto", token)

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	w.WriteField("chat_id", chatID)
	w.WriteField("caption", caption)
	part, _ := w.CreateFormFile("photo", "snapshot.jpg")
	part.Write(photo)
	w.Close()

	resp, err := client.Post(apiURL, w.FormDataContentType(), &buf)
	if err != nil {
		slog.Error("telegram sendPhoto failed", "component", "opencode", "chatID", chatID, "err", err)
		return
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		slog.Error("telegram sendPhoto error", "component", "opencode", "chatID", chatID, "status", resp.StatusCode, "body", string(body))
		return
	}
	slog.Info("telegram sendPhoto sent", "component", "opencode", "chatID", chatID)
}
