package opencode

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestAcceptDiscordMessage(t *testing.T) {
	const bot = "B1"
	cases := []struct {
		name         string
		in           discordInbound
		allowedUser  string
		allowedGuild string
		wantText     string
		wantOK       bool
	}{
		{
			name:        "DM from allowlisted user accepted",
			in:          discordInbound{authorID: "U1", botUserID: bot, content: "hello lamp"},
			allowedUser: "U1",
			wantText:    "hello lamp",
			wantOK:      true,
		},
		{
			name:        "DM from other user rejected",
			in:          discordInbound{authorID: "U2", botUserID: bot, content: "hi"},
			allowedUser: "U1",
		},
		{
			name: "empty allowlist rejects everyone (closed by default)",
			in:   discordInbound{authorID: "U1", botUserID: bot, content: "hi"},
		},
		{
			name:        "bot author rejected (loop guard)",
			in:          discordInbound{authorID: "U1", authorBot: true, botUserID: bot, content: "hi"},
			allowedUser: "U1",
		},
		{
			name:        "own message rejected",
			in:          discordInbound{authorID: bot, botUserID: bot, content: "hi"},
			allowedUser: bot,
		},
		{
			name:         "guild message with mention accepted, mention stripped",
			in:           discordInbound{authorID: "U1", botUserID: bot, guildID: "G1", content: "<@B1> ping", mentionsBot: true},
			allowedUser:  "U1",
			allowedGuild: "G1",
			wantText:     "ping",
			wantOK:       true,
		},
		{
			name:         "guild nickname-form mention stripped",
			in:           discordInbound{authorID: "U1", botUserID: bot, guildID: "G1", content: "<@!B1> ping", mentionsBot: true},
			allowedUser:  "U1",
			allowedGuild: "G1",
			wantText:     "ping",
			wantOK:       true,
		},
		{
			name:         "guild message without mention rejected",
			in:           discordInbound{authorID: "U1", botUserID: bot, guildID: "G1", content: "ping"},
			allowedUser:  "U1",
			allowedGuild: "G1",
		},
		{
			name:         "wrong guild rejected",
			in:           discordInbound{authorID: "U1", botUserID: bot, guildID: "G2", content: "<@B1> ping", mentionsBot: true},
			allowedUser:  "U1",
			allowedGuild: "G1",
		},
		{
			name:        "guild message rejected when no guild configured",
			in:          discordInbound{authorID: "U1", botUserID: bot, guildID: "G1", content: "<@B1> ping", mentionsBot: true},
			allowedUser: "U1",
		},
		{
			name:         "bare mention rejected (nothing left to say)",
			in:           discordInbound{authorID: "U1", botUserID: bot, guildID: "G1", content: "<@B1>", mentionsBot: true},
			allowedUser:  "U1",
			allowedGuild: "G1",
		},
		{
			name:        "empty DM content rejected",
			in:          discordInbound{authorID: "U1", botUserID: bot, content: "   "},
			allowedUser: "U1",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			text, ok := acceptDiscordMessage(tc.in, tc.allowedUser, tc.allowedGuild)
			if ok != tc.wantOK || text != tc.wantText {
				t.Errorf("acceptDiscordMessage = (%q, %v), want (%q, %v)", text, ok, tc.wantText, tc.wantOK)
			}
		})
	}
}

func TestDiscordTurnText(t *testing.T) {
	got := discordTurnText("leo", "42", "hello")
	want := "[discord] Message from leo [id:42]:\nhello"
	if got != want {
		t.Errorf("discordTurnText = %q, want %q", got, want)
	}
	got = discordTurnText("", "42", "hi")
	if got != "[discord] Message from unknown [id:42]:\nhi" {
		t.Errorf("empty-username turn text = %q", got)
	}
}

func TestChunkDiscordMessage(t *testing.T) {
	if got := chunkDiscordMessage("hello", 2000); len(got) != 1 || got[0] != "hello" {
		t.Errorf("short text chunks = %v", got)
	}

	exact := strings.Repeat("a", 10)
	if got := chunkDiscordMessage(exact, 10); len(got) != 1 || got[0] != exact {
		t.Errorf("exact-limit chunks = %v", got)
	}

	over := strings.Repeat("a", 11)
	got := chunkDiscordMessage(over, 10)
	if len(got) != 2 || got[0] != strings.Repeat("a", 10) || got[1] != "a" {
		t.Errorf("boundary chunks = %v", got)
	}

	text := "first line\nsecond line"
	got = chunkDiscordMessage(text, 15)
	if len(got) != 2 || got[0] != "first line" || got[1] != "second line" {
		t.Errorf("newline chunks = %v, want [first line, second line]", got)
	}

	// Limit is in runes, not bytes (multibyte content must not be split mid-rune).
	uni := strings.Repeat("é", 11)
	got = chunkDiscordMessage(uni, 10)
	if len(got) != 2 || got[0] != strings.Repeat("é", 10) || got[1] != "é" {
		t.Errorf("rune chunks = %v", got)
	}

	// Every chunk must fit within the limit.
	long := strings.Repeat("word word word\n", 300)
	for i, c := range chunkDiscordMessage(long, 2000) {
		if n := len([]rune(c)); n > 2000 {
			t.Errorf("chunk %d has %d runes, over the 2000 limit", i, n)
		}
	}
}

func TestDiscordRunMapRoundTrip(t *testing.T) {
	s := &OpenCodeService{discordRuns: make(map[string]string)}

	s.markDiscordRun("run-1", "C42")
	if !s.hasDiscordRun("run-1") || !s.hasDiscordRun("run-1") {
		t.Errorf("hasDiscordRun should be true before consume (non-consuming peek)")
	}
	if got := s.consumeDiscordRun("run-1"); got != "C42" {
		t.Fatalf("consumeDiscordRun = %q, want C42", got)
	}
	if s.hasDiscordRun("run-1") {
		t.Errorf("run not cleared after consume")
	}
	if got := s.consumeDiscordRun("run-1"); got != "" {
		t.Errorf("second consume = %q, want empty (miss)", got)
	}
	s.markDiscordRun("run-2", "")
	if s.hasDiscordRun("run-2") {
		t.Errorf("empty-channel run should not be recorded")
	}
	s.markDiscordRun("", "C1")
	if s.hasDiscordRun("") {
		t.Errorf("empty runID should not be recorded")
	}
}

// TestInjectDiscordTurn drives an accepted message through injection: the
// turn text goes out via the send seam, the run is tracked in discordRuns and
// marked silent (no TTS).
func TestInjectDiscordTurn(t *testing.T) {
	type sent struct{ text, reqID, runID string }
	injected := make(chan sent, 1)
	s := &OpenCodeService{
		config:      &config.Config{},
		discordRuns: make(map[string]string),
		silentRuns:  make(map[string]bool),
	}
	s.discordSendTurn = func(text, reqID, runID string) error {
		injected <- sent{text: text, reqID: reqID, runID: runID}
		return nil
	}

	turnText := discordTurnText("leo", "42", "hello lamp")
	s.injectDiscordTurn(context.Background(), turnText, "C9")

	var got sent
	select {
	case got = <-injected:
	case <-time.After(5 * time.Second):
		t.Fatal("no turn injected within 5s")
	}
	if got.text != "[discord] Message from leo [id:42]:\nhello lamp" {
		t.Errorf("injected text = %q", got.text)
	}
	if !s.IsSilentRun(got.runID) {
		t.Errorf("run %q not marked silent — Discord replies must not hit TTS", got.runID)
	}
	if ch := s.consumeDiscordRun(got.runID); ch != "C9" {
		t.Errorf("discord run channel = %q, want C9", ch)
	}
}

// TestDiscordReplyRoutingEmitFinal exercises the reply seam end-to-end: a
// Discord-marked run finishing via emitFinal must post the final text —
// [HW:/...] markers and audio tags stripped — to the originating channel via
// the send seam.
func TestDiscordReplyRoutingEmitFinal(t *testing.T) {
	type msg struct{ channelID, text string }
	sends := make(chan msg, 4)
	s := &OpenCodeService{
		config:      &config.Config{},
		discordRuns: make(map[string]string),
		silentRuns:  make(map[string]bool),
	}
	s.discordSendMessage = func(channelID, text string) error {
		sends <- msg{channelID: channelID, text: text}
		return nil
	}
	runID := "device-chat-1-123"
	s.markDiscordRun(runID, "C9")
	s.setCurrentRunID(runID)
	s.assistantParts = []string{"[HW:/led/set:{\"r\":1}] Done! [laugh]"}

	s.emitFinal(func(domain.WSEvent) {})

	if s.hasDiscordRun(runID) {
		t.Errorf("discord run not consumed by emitFinal")
	}
	select {
	case got := <-sends:
		if got.channelID != "C9" || got.text != "Done!" {
			t.Errorf("reply = %+v, want channel C9, text %q (markers stripped)", got, "Done!")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no discord reply sent within 5s")
	}
}

// TestDiscordReplyChunkedOnEmitFinal: a reply over 2000 chars is delivered in
// order as multiple messages, each within Discord's limit.
func TestDiscordReplyChunkedOnEmitFinal(t *testing.T) {
	var mu sync.Mutex
	var texts []string
	done := make(chan struct{})
	s := &OpenCodeService{
		config:      &config.Config{},
		discordRuns: make(map[string]string),
		silentRuns:  make(map[string]bool),
	}
	long := strings.Repeat("a", 2000) + "\n" + strings.Repeat("b", 100)
	s.discordSendMessage = func(_ string, text string) error {
		mu.Lock()
		texts = append(texts, text)
		n := len(texts)
		mu.Unlock()
		if n == 2 {
			close(done)
		}
		return nil
	}
	runID := "device-chat-2-456"
	s.markDiscordRun(runID, "C9")
	s.setCurrentRunID(runID)
	s.assistantParts = []string{long}

	s.emitFinal(func(domain.WSEvent) {})

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("chunked reply not fully sent within 5s")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(texts) != 2 || texts[0] != strings.Repeat("a", 2000) || texts[1] != strings.Repeat("b", 100) {
		t.Errorf("chunks = %d [%d, %d runes], want 2 [2000, 100]", len(texts), len([]rune(texts[0])), len([]rune(texts[len(texts)-1])))
	}
}

// TestDiscordRunConsumedOnError: a failed turn consumes the tracker (no leak)
// and posts nothing.
func TestDiscordRunConsumedOnError(t *testing.T) {
	s := &OpenCodeService{
		config:      &config.Config{},
		discordRuns: make(map[string]string),
		silentRuns:  make(map[string]bool),
	}
	s.discordSendMessage = func(string, string) error {
		t.Error("no reply must be sent for a failed turn")
		return nil
	}
	runID := "device-chat-3-789"
	s.markDiscordRun(runID, "C9")
	s.setCurrentRunID(runID)

	s.handleError("boom", func(domain.WSEvent) {})

	if s.hasDiscordRun(runID) {
		t.Errorf("discord run not consumed by handleError")
	}
}
