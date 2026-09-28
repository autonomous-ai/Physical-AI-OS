package opencode

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"github.com/bwmarrin/discordgo"
)

// Like the telegram loop (telegram_poll.go), it is started from StartWS and lives inside this
// service's lifecycle — it runs ONLY while opencode is the active runtime, so it can never fight
// another runtime's bot session for the same token.

const (
	// discordNoTokenWait is the idle recheck interval while no bot token is
	// configured (the user may save one later — keep checking).
	discordNoTokenWait = 30 * time.Second

	// discordErrorWait is the backoff after a failed session open so a broken
	// token or flaky network cannot hot-loop against the Discord gateway.
	discordErrorWait = 15 * time.Second

	// discordBusyPoll is how often an accepted message rechecks IsBusy()
	// before injecting its turn (mirrors telegramBusyPoll).
	discordBusyPoll = 500 * time.Millisecond

	// discordTypingLifetime caps a typing keeper — matches the gatewayd turn
	// timeout so a wedged turn cannot leave the channel "typing…" forever.
	discordTypingLifetime = 10 * time.Minute

	// discordTypingInterval is the keeper refire period (Discord typing lasts ~10 s).
	discordTypingInterval = 8 * time.Second

	// discordMaxMessageLen is Discord's hard per-message character limit;
	// longer replies are chunked (newline boundaries preferred).
	discordMaxMessageLen = 2000
)

// discordInbound is the transport-independent view of one MessageCreate event
// that the pure accept filter operates on (hermetic tests build it directly).
type discordInbound struct {
	authorID    string
	authorBot   bool // author is a bot (includes our own messages — loop guard)
	botUserID   string
	guildID     string // empty for DMs
	channelID   string
	content     string
	mentionsBot bool // the bot user appears in the message mentions
}

// discordMentionRE matches a mention of one user id (<@123> / <@!123> nickname
// form), built per bot id by stripDiscordMention.
func discordMentionRE(botUserID string) *regexp.Regexp {
	return regexp.MustCompile(`<@!?` + regexp.QuoteMeta(botUserID) + `>`)
}

// stripDiscordMention removes every mention of the bot user from the text
// (guild messages must @mention the bot to be accepted; the mention itself is
// noise for the agent).
func stripDiscordMention(content, botUserID string) string {
	if botUserID == "" {
		return strings.TrimSpace(content)
	}
	return strings.TrimSpace(discordMentionRE(botUserID).ReplaceAllString(content, ""))
}

// acceptDiscordMessage is the pure (no-I/O) accept filter.
func acceptDiscordMessage(in discordInbound, allowedUser, allowedGuild string) (string, bool) {
	if in.authorBot || in.authorID == in.botUserID {
		return "", false
	}
	if allowedUser == "" || in.authorID != allowedUser {
		return "", false
	}
	isDM := in.guildID == ""
	if !isDM {
		if allowedGuild == "" || in.guildID != allowedGuild {
			return "", false
		}
		if !in.mentionsBot {
			return "", false
		}
	}
	text := stripDiscordMention(in.content, in.botUserID)
	if text == "" {
		return "", false
	}
	return text, true
}

// discordTurnText builds the sender-metadata prefix so the agent knows who is
// talking and on which channel — same shape as the telegram/slack paths.
func discordTurnText(username, userID, text string) string {
	if username == "" {
		username = "unknown"
	}
	return fmt.Sprintf("[discord] Message from %s [id:%s]:\n%s", username, userID, text)
}

// chunkDiscordMessage splits text into Discord-sendable chunks of at most
// limit characters (runes — Discord counts characters, not bytes), preferring
// to cut at the last newline inside the window so paragraphs stay whole.
func chunkDiscordMessage(text string, limit int) []string {
	runes := []rune(text)
	if len(runes) <= limit {
		return []string{text}
	}
	var out []string
	for len(runes) > 0 {
		if len(runes) <= limit {
			if c := strings.TrimRight(string(runes), "\n"); c != "" {
				out = append(out, c)
			}
			break
		}
		cut := limit
		for i := limit; i >= 1; i-- {
			if runes[i-1] == '\n' {
				cut = i
				break
			}
		}
		if c := strings.TrimRight(string(runes[:cut]), "\n"); c != "" {
			out = append(out, c)
		}
		runes = runes[cut:]
		for len(runes) > 0 && runes[0] == '\n' {
			runes = runes[1:]
		}
	}
	return out
}

// startDiscordBot runs the device-owned Discord gateway session until ctx is
// cancelled.
func (s *OpenCodeService) startDiscordBot(ctx context.Context) {
	slog.Info("discord bot loop started", "component", "opencode")
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}
		// Read creds fresh every attempt: config may be saved at any time and
		// the loop must pick that up without a restart.
		token := s.config.DiscordBotToken
		if token == "" {
			if !sleepCtx(ctx, discordNoTokenWait) {
				return
			}
			continue
		}
		sess, err := s.openDiscordSession(ctx, token)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Warn("discord session open failed", "component", "opencode", "error", err)
			if !sleepCtx(ctx, discordErrorWait) {
				return
			}
			continue
		}
		<-ctx.Done()
		s.setDiscordSession(nil)
		if err := sess.Close(); err != nil {
			slog.Debug("discord session close failed", "component", "opencode", "error", err)
		}
		return
	}
}

// openDiscordSession builds, wires and opens one discordgo session: DM +
// guild-message + message-content intents, the MessageCreate handler, and the
// service-held handle the reply/typing senders use.
func (s *OpenCodeService) openDiscordSession(ctx context.Context, token string) (*discordgo.Session, error) {
	sess, err := discordgo.New("Bot " + token)
	if err != nil {
		return nil, fmt.Errorf("build discord session: %w", err)
	}
	sess.Identify.Intents = discordgo.IntentsDirectMessages |
		discordgo.IntentsGuildMessages |
		discordgo.IntentMessageContent
	sess.AddHandler(func(_ *discordgo.Session, m *discordgo.MessageCreate) {
		s.handleDiscordMessage(ctx, sess, m)
	})
	if err := sess.Open(); err != nil {
		return nil, fmt.Errorf("open discord gateway: %w", err)
	}
	s.setDiscordSession(sess)
	botID := ""
	if sess.State != nil && sess.State.User != nil {
		botID = sess.State.User.ID
	}
	slog.Info("discord session open", "component", "opencode", "botUserId", botID)
	return sess, nil
}

// setDiscordSession / getDiscordSession guard the session handle so the reply
// sender (translator.go emitFinal, off the handler goroutine) sees a
// consistent value.
func (s *OpenCodeService) setDiscordSession(sess *discordgo.Session) {
	s.discordMu.Lock()
	s.discordSession = sess
	s.discordMu.Unlock()
}

func (s *OpenCodeService) getDiscordSession() *discordgo.Session {
	s.discordMu.Lock()
	defer s.discordMu.Unlock()
	return s.discordSession
}

// handleDiscordMessage filters one MessageCreate and injects the accepted
// message as a chat turn.
func (s *OpenCodeService) handleDiscordMessage(ctx context.Context, sess *discordgo.Session, m *discordgo.MessageCreate) {
	if m.Author == nil {
		return
	}
	botUserID := ""
	if sess.State != nil && sess.State.User != nil {
		botUserID = sess.State.User.ID
	}
	mentionsBot := false
	for _, u := range m.Mentions {
		if u != nil && u.ID == botUserID {
			mentionsBot = true
			break
		}
	}
	in := discordInbound{
		authorID:    m.Author.ID,
		authorBot:   m.Author.Bot,
		botUserID:   botUserID,
		guildID:     m.GuildID,
		channelID:   m.ChannelID,
		content:     m.Content,
		mentionsBot: mentionsBot,
	}
	text, ok := acceptDiscordMessage(in, s.config.DiscordUserID, s.config.DiscordGuildID)
	if !ok {
		slog.Debug("discord message skipped",
			"component", "opencode", "authorId", m.Author.ID, "guildId", m.GuildID)
		return
	}
	turnText := discordTurnText(m.Author.Username, m.Author.ID, text)
	s.injectDiscordTurn(ctx, turnText, m.ChannelID)
}

// injectDiscordTurn waits for the agent to go idle, then sends the message as
// a regular chat turn with flow source "discord" (chat_input / chat_send flow
// events fire as usual, so Flow Monitor shows the origin).
func (s *OpenCodeService) injectDiscordTurn(ctx context.Context, text, channelID string) {
	for s.IsBusy() {
		if !sleepCtx(ctx, discordBusyPoll) {
			return
		}
	}
	reqID, runID := s.NextChatRunID()
	s.markDiscordRun(runID, channelID)
	s.MarkSilentRun(runID)
	send := s.discordSendTurn
	if send == nil {
		send = func(text, reqID, runID string) error {
			_, err := s.sendChat(text, nil, reqID, runID, "discord")
			return err
		}
	}
	if err := send(text, reqID, runID); err != nil {
		// Un-mark so the trackers don't leak.
		s.consumeDiscordRun(runID)
		s.ConsumeSilentRun(runID)
		slog.Error("discord turn injection failed",
			"component", "opencode", "runID", runID, "channelID", channelID, "error", err)
		return
	}
	go s.discordTypingKeeper(ctx, channelID, runID)
}

// discordTypingKeeper fires ChannelTyping immediately and then every
// discordTypingInterval (the native indicator lasts ~10 s) until the run is
// consumed by emitFinal or handleError.
func (s *OpenCodeService) discordTypingKeeper(ctx context.Context, channelID, runID string) {
	deadline := time.Now().Add(discordTypingLifetime)
	for {
		if !s.hasDiscordRun(runID) || time.Now().After(deadline) {
			return
		}
		s.sendDiscordTyping(channelID)
		if !sleepCtx(ctx, discordTypingInterval) {
			return
		}
	}
}

// sendDiscordTyping triggers one native typing indicator on channelID.
func (s *OpenCodeService) sendDiscordTyping(channelID string) {
	sess := s.getDiscordSession()
	if sess == nil {
		return
	}
	if err := sess.ChannelTyping(channelID); err != nil {
		slog.Debug("discord ChannelTyping failed", "component", "opencode", "error", err)
	}
}

// finishDiscordTurn posts a reply to the originating channel, chunked at
// Discord's 2000-character message limit.
// Called from emitFinal (goroutine — the WS read loop must not block on the Discord API).
func (s *OpenCodeService) finishDiscordTurn(channelID, reply string) {
	if reply == "" {
		return
	}
	send := s.discordSendMessage
	if send == nil {
		send = func(channelID, text string) error {
			sess := s.getDiscordSession()
			if sess == nil {
				return fmt.Errorf("discord session not open")
			}
			_, err := sess.ChannelMessageSend(channelID, text)
			return err
		}
	}
	for _, chunk := range chunkDiscordMessage(reply, discordMaxMessageLen) {
		if err := send(channelID, chunk); err != nil {
			slog.Error("discord reply send failed",
				"component", "opencode", "channelID", channelID, "error", err)
			return
		}
	}
	slog.Info("discord reply posted", "component", "opencode", "channelID", channelID)
}
