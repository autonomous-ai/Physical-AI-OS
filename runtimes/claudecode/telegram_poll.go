package claudecode

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// The native telegram channel plugin is deliberately not used: it logs nothing, drops
// non-allowlisted senders silently and can die on a bridge restart race.

const (
	// telegramOffsetFile persists the next getUpdates offset across restarts so
	// already-processed updates are not re-injected.
	telegramOffsetFile = "/root/.claudecode/telegram_offset.json"

	// telegramAPIBaseDefault is the production Bot API host.
	telegramAPIBaseDefault = "https://api.telegram.org"

	// telegramPollTimeoutS is the getUpdates long-poll window in seconds (the
	// server holds the request until an update arrives or the window expires).
	telegramPollTimeoutS = 50

	// telegramNoTokenWait is the idle recheck interval while no bot token is
	// configured (the user may save one later — keep checking).
	telegramNoTokenWait = 30 * time.Second

	// telegramErrorWait is the backoff after an HTTP/network error so a broken
	// token or flaky network cannot hot-loop against the Bot API.
	telegramErrorWait = 5 * time.Second

	// telegramBusyPoll is how often an accepted message rechecks IsBusy()
	// before injecting its turn.
	telegramBusyPoll = 500 * time.Millisecond
)

// tgUpdate / tgMessage / tgChat / tgUser are the minimal Bot API getUpdates
// shapes this loop consumes.
type tgUpdate struct {
	UpdateID int64      `json:"update_id"`
	Message  *tgMessage `json:"message"`
}

type tgMessage struct {
	Text string  `json:"text"`
	Chat tgChat  `json:"chat"`
	From *tgUser `json:"from"`
}

type tgChat struct {
	ID   int64  `json:"id"`
	Type string `json:"type"`
}

type tgUser struct {
	ID        int64  `json:"id"`
	FirstName string `json:"first_name"`
	LastName  string `json:"last_name"`
	Username  string `json:"username"`
}

// label renders the sender for the turn header, mirroring how openclaw's
// telegram plugin surfaces who is talking (name + @username + numeric id).
func (u *tgUser) label() string {
	if u == nil {
		return "unknown"
	}
	name := strings.TrimSpace(u.FirstName + " " + u.LastName)
	if name == "" {
		name = "unknown"
	}
	if u.Username != "" {
		name += " (@" + u.Username + ")"
	}
	return fmt.Sprintf("%s [id:%d]", name, u.ID)
}

type tgGetUpdatesResp struct {
	OK          bool       `json:"ok"`
	Description string     `json:"description"`
	Result      []tgUpdate `json:"result"`
}

// telegramOffsetState is the schema of telegramOffsetFile.
type telegramOffsetState struct {
	Offset int64 `json:"offset"`
}

// startTelegramPoll runs the device-owned Telegram receive loop until ctx is
// cancelled. Runs only while this runtime is active: Telegram 409s concurrent pollers.
func (s *ClaudeCodeService) startTelegramPoll(ctx context.Context) {
	offset := s.loadTelegramOffset()
	// Client timeout must exceed the long-poll window or every healthy idle
	// poll would abort client-side at telegramPollTimeoutS.
	client := &http.Client{Timeout: 70 * time.Second}
	slog.Info("telegram poll loop started", "component", "claudecode", "offset", offset)
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		// Read creds fresh every iteration: config may be saved or rotated at
		// any time and the loop must pick that up without a restart.
		token := s.config.TelegramBotToken
		if token == "" {
			if !sleepCtx(ctx, telegramNoTokenWait) {
				return
			}
			continue
		}
		updates, err := s.fetchTelegramUpdates(ctx, client, token, offset)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Warn("telegram getUpdates failed", "component", "claudecode", "error", err)
			if !sleepCtx(ctx, telegramErrorWait) {
				return
			}
			continue
		}
		if len(updates) == 0 {
			continue
		}
		allowedUser := s.config.TelegramUserID
		for _, u := range updates {
			if u.UpdateID >= offset {
				offset = u.UpdateID + 1
			}
			if ctx.Err() != nil {
				s.saveTelegramOffset(offset)
				return
			}
			s.handleTelegramUpdate(ctx, u, allowedUser)
		}
		s.saveTelegramOffset(offset)
	}
}

// handleTelegramUpdate filters one update and injects the accepted message as
// a chat turn.
// Everything else is skipped at debug level — the offset was already advanced by the caller, so
// rejects are never re-delivered.
func (s *ClaudeCodeService) handleTelegramUpdate(ctx context.Context, u tgUpdate, allowedUser string) {
	msg := u.Message
	if msg == nil || strings.TrimSpace(msg.Text) == "" {
		slog.Debug("telegram update skipped (no text message)", "component", "claudecode", "updateId", u.UpdateID)
		return
	}
	if msg.Chat.Type != "private" {
		slog.Debug("telegram update skipped (not a private chat)",
			"component", "claudecode", "updateId", u.UpdateID, "chatType", msg.Chat.Type)
		return
	}
	fromID := ""
	if msg.From != nil {
		fromID = strconv.FormatInt(msg.From.ID, 10)
	}
	if allowedUser == "" || fromID != allowedUser {
		slog.Debug("telegram update skipped (sender not allowlisted)",
			"component", "claudecode", "updateId", u.UpdateID, "fromId", fromID)
		return
	}
	chatID := strconv.FormatInt(msg.Chat.ID, 10)
	s.upsertTelegramTarget(chatID, msg.Chat.Type)
	if s.handleTelegramCoding(ctx, msg.Text, chatID) {
		return
	}
	turnText := fmt.Sprintf("[telegram] Message from %s:\n%s", msg.From.label(), msg.Text)
	s.injectTelegramTurn(ctx, turnText, chatID)
}

// injectTelegramTurn waits for the agent to go idle, then sends the message as
// a regular chat turn with flow source "telegram" (chat_input / chat_send flow
// events fire as usual, so Flow Monitor shows the origin).
func (s *ClaudeCodeService) injectTelegramTurn(ctx context.Context, text, chatID string) {
	for s.IsBusy() {
		if !sleepCtx(ctx, telegramBusyPoll) {
			return
		}
	}
	reqID, runID := s.NextChatRunID()
	s.markTelegramRun(runID, chatID)
	s.MarkSilentRun(runID)
	send := s.telegramSendTurn
	if send == nil {
		send = func(text, reqID, runID string) error {
			_, err := s.sendChat(text, nil, reqID, runID, "telegram")
			return err
		}
	}
	if err := send(text, reqID, runID); err != nil {
		// Un-mark so the trackers don't leak.
		s.consumeTelegramRun(runID)
		s.ConsumeSilentRun(runID)
		slog.Error("telegram turn injection failed",
			"component", "claudecode", "runID", runID, "chatID", chatID, "error", err)
		return
	}
	go s.telegramTypingKeeper(ctx, chatID, runID)
}

// telegramTypingLifetime caps a typing keeper — matches the gatewayd turn
// timeout so a wedged turn cannot leave the chat "typing…" forever.
const telegramTypingLifetime = 10 * time.Minute

// telegramTypingKeeper fires sendChatAction(typing) immediately and then every
// 4s (the indicator expires after ~5s) until the run is consumed by emitFinal
// or handleError.
func (s *ClaudeCodeService) telegramTypingKeeper(ctx context.Context, chatID, runID string) {
	deadline := time.Now().Add(telegramTypingLifetime)
	for {
		if !s.hasTelegramRun(runID) || time.Now().After(deadline) {
			return
		}
		s.sendTelegramTyping(ctx, chatID)
		if !sleepCtx(ctx, 4*time.Second) {
			return
		}
	}
}

// sendTelegramTyping posts one sendChatAction "typing" for chatID.
func (s *ClaudeCodeService) sendTelegramTyping(ctx context.Context, chatID string) {
	token := s.config.TelegramBotToken
	if token == "" {
		return
	}
	base := s.telegramAPIBase
	if base == "" {
		base = telegramAPIBaseDefault
	}
	url := fmt.Sprintf("%s/bot%s/sendChatAction?chat_id=%s&action=typing", base, token, chatID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		slog.Debug("telegram sendChatAction failed", "component", "claudecode", "error", err)
		return
	}
	_ = resp.Body.Close()
}

// fetchTelegramUpdates performs one getUpdates long-poll.
func (s *ClaudeCodeService) fetchTelegramUpdates(ctx context.Context, client *http.Client, token string, offset int64) ([]tgUpdate, error) {
	base := s.telegramAPIBase
	if base == "" {
		base = telegramAPIBaseDefault
	}
	url := fmt.Sprintf("%s/bot%s/getUpdates?timeout=%d&offset=%d", base, token, telegramPollTimeoutS, offset)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("build getUpdates request: %w", err)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("getUpdates: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, fmt.Errorf("read getUpdates response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("getUpdates status %d: %s", resp.StatusCode, truncRunes(string(body), 200))
	}
	var parsed tgGetUpdatesResp
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("parse getUpdates response: %w", err)
	}
	if !parsed.OK {
		return nil, fmt.Errorf("getUpdates not ok: %s", parsed.Description)
	}
	return parsed.Result, nil
}

// telegramOffsetFilePath returns the offset state path (test override).
func (s *ClaudeCodeService) telegramOffsetFilePath() string {
	if s.telegramOffsetPath != "" {
		return s.telegramOffsetPath
	}
	return telegramOffsetFile
}

// loadTelegramOffset reads the persisted next-update offset.
func (s *ClaudeCodeService) loadTelegramOffset() int64 {
	data, err := os.ReadFile(s.telegramOffsetFilePath())
	if err != nil {
		if !os.IsNotExist(err) {
			slog.Warn("telegram offset read failed", "component", "claudecode", "error", err)
		}
		return 0
	}
	var st telegramOffsetState
	if err := json.Unmarshal(data, &st); err != nil {
		slog.Warn("telegram offset parse failed", "component", "claudecode", "error", err)
		return 0
	}
	return st.Offset
}

// saveTelegramOffset atomically persists the next-update offset (temp +
// rename) so a crash between polls cannot corrupt the state file.
func (s *ClaudeCodeService) saveTelegramOffset(offset int64) {
	path := s.telegramOffsetFilePath()
	data, err := json.Marshal(telegramOffsetState{Offset: offset})
	if err != nil {
		slog.Warn("telegram offset marshal failed", "component", "claudecode", "error", err)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		slog.Warn("telegram offset dir create failed", "component", "claudecode", "error", err)
		return
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		slog.Warn("telegram offset write failed", "component", "claudecode", "error", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		slog.Warn("telegram offset rename failed", "component", "claudecode", "error", err)
	}
}
