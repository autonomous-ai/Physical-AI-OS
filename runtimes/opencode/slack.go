package opencode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// Compile-time check: *OpenCodeService implements domain.SlackBridge, which is
// what the slack_event MQTT handler type-asserts to route inbound events here.
var _ domain.SlackBridge = (*OpenCodeService)(nil)

const (
	// slackBusyPoll is how often an accepted Slack message rechecks IsBusy()
	// before injecting its turn (mirrors telegramBusyPoll).
	slackBusyPoll = 500 * time.Millisecond

	// slackBusyWaitMax caps the busy wait so a wedged agent cannot pin the
	// injection goroutine forever; past it the message is dropped (Slack will
	// not re-deliver — the MQTT handler already acked the event).
	slackBusyWaitMax = 2 * time.Minute
)

// slackRun records where an inbound Slack turn came from so emitFinal can post
// the reply in the same channel/thread and clear the ack reaction.
type slackRun struct {
	channel   string
	threadTS  string
	messageTS string // the user message ts — used to add/remove the eyes reaction
}

// slackEnvelope is the outer Slack Events API shape the bff proxy forwards
// verbatim.
type slackEnvelope struct {
	Type      string     `json:"type"`      // "url_verification" | "event_callback"
	Challenge string     `json:"challenge"` // url_verification only
	Event     slackEvent `json:"event"`     // event_callback only
}

type slackEvent struct {
	Type     string `json:"type"`      // "message" | "app_mention" | ...
	Subtype  string `json:"subtype"`   // "message_changed", "bot_message", ...
	Text     string `json:"text"`      //
	User     string `json:"user"`      // Slack user id (empty for bot/system events)
	BotID    string `json:"bot_id"`    // set on bot messages — loop guard
	Channel  string `json:"channel"`   //
	TS       string `json:"ts"`        //
	ThreadTS string `json:"thread_ts"` //
}

// slackMentionRE strips a leading bot mention (<@U123ABC>) from app_mention text.
var slackMentionRE = regexp.MustCompile(`^\s*<@[A-Z0-9]+>\s*`)

// parsedSlackMsg is a real user message extracted from a Slack event.
type parsedSlackMsg struct {
	text      string
	user      string
	channel   string
	threadTS  string
	messageTS string
}

// parseSlackInbound is the pure (no-I/O) decode of a forwarded Slack event,
// identical in semantics to hermes' parseSlackInbound:
// - challenge != "" — a url_verification handshake.
func parseSlackInbound(body, allowedUser string) (challenge string, msg *parsedSlackMsg, err error) {
	var env slackEnvelope
	if uerr := json.Unmarshal([]byte(body), &env); uerr != nil {
		return "", nil, fmt.Errorf("parse slack event: %w", uerr)
	}
	if env.Type == "url_verification" {
		return env.Challenge, nil, nil
	}
	if env.Type != "event_callback" {
		return "", nil, nil
	}
	ev := env.Event
	if ev.Type != "message" && ev.Type != "app_mention" {
		return "", nil, nil
	}
	if ev.BotID != "" || ev.Subtype != "" || ev.User == "" {
		return "", nil, nil
	}
	if allowedUser != "" && ev.User != allowedUser {
		return "", nil, nil
	}
	text := strings.TrimSpace(slackMentionRE.ReplaceAllString(ev.Text, ""))
	if text == "" {
		return "", nil, nil
	}
	return "", &parsedSlackMsg{text: text, user: ev.User, channel: ev.Channel, threadTS: ev.ThreadTS, messageTS: ev.TS}, nil
}

// HandleInboundSlack implements domain.SlackBridge.
// Injection happens in a goroutine (the agent may be busy and opencode correlates turns by a single
// in-flight runID, so the turn waits for idle like the telegram path) — handled == true therefore
// means "accepted for injection", and the MQTT handler acks immediately.
func (s *OpenCodeService) HandleInboundSlack(in domain.SlackInbound) (string, bool, error) {
	challenge, msg, err := parseSlackInbound(in.Body, s.config.SlackUserID)
	if err != nil {
		return "", false, err
	}
	if challenge != "" {
		slog.Info("slack: url_verification challenge", "component", "opencode")
		return challenge, false, nil
	}
	if msg == nil {
		return "", false, nil
	}

	threadTS := msg.threadTS
	if threadTS == "" {
		threadTS = msg.messageTS
	}
	turnText := fmt.Sprintf("[slack] Message from <@%s> [channel:%s]:\n%s", msg.user, msg.channel, msg.text)
	go s.injectSlackTurn(turnText, slackRun{channel: msg.channel, threadTS: threadTS, messageTS: msg.messageTS})
	slog.Info("slack: inbound accepted for opencode turn", "component", "opencode", "channel", msg.channel)
	return "", true, nil
}

// injectSlackTurn waits for the agent to go idle, then sends the message as a
// regular chat turn with flow source "slack" (chat_input / chat_send flow
// events fire as usual, so Flow Monitor shows the origin).
func (s *OpenCodeService) injectSlackTurn(text string, origin slackRun) {
	deadline := time.Now().Add(slackBusyWaitMax)
	for s.IsBusy() {
		if time.Now().After(deadline) {
			slog.Warn("slack turn dropped (agent busy past cap)",
				"component", "opencode", "channel", origin.channel, "cap", slackBusyWaitMax)
			return
		}
		time.Sleep(slackBusyPoll)
	}
	reqID, runID := s.NextChatRunID()
	s.markSlackRun(runID, origin)
	s.MarkSilentRun(runID)
	send := s.slackSendTurn
	if send == nil {
		send = func(text, reqID, runID string) error {
			_, err := s.sendChat(text, nil, reqID, runID, "slack")
			return err
		}
	}
	if err := send(text, reqID, runID); err != nil {
		// Un-mark so the trackers don't leak.
		s.consumeSlackRun(runID)
		s.ConsumeSilentRun(runID)
		slog.Error("slack turn injection failed",
			"component", "opencode", "runID", runID, "channel", origin.channel, "error", err)
		return
	}
	if origin.messageTS != "" {
		if err := s.setSlackReaction(true, origin.channel, origin.messageTS, slackAckReaction); err != nil {
			slog.Debug("slack: ack reaction failed (non-fatal)", "component", "opencode", "err", err)
		}
	}
}

// IsSlackOriginRun implements domain.SlackBridge — non-consuming peek so the
// SSE handler's mid-turn gates can see a Slack turn.
func (s *OpenCodeService) IsSlackOriginRun(runID string) bool {
	return s.hasSlackRun(runID)
}

// StreamSlackDelta implements domain.SlackBridge — intentionally a no-op:
// opencode run emits the reply whole (no delta stream), so there is nothing to
// stream progressively; the full reply is posted once by emitFinal.
func (s *OpenCodeService) StreamSlackDelta(string, string) {}

// DeliverSlackReply implements domain.SlackBridge.
func (s *OpenCodeService) DeliverSlackReply(runID, text string) error {
	o, ok := s.consumeSlackRun(runID)
	if !ok {
		return nil
	}
	s.finishSlackTurn(o, text)
	return nil
}

// finishSlackTurn finalizes a Slack-origin turn: clears the eyes ack reaction
// (best-effort) and posts the reply to the originating channel/thread when
// non-empty.
func (s *OpenCodeService) finishSlackTurn(o slackRun, reply string) {
	if o.messageTS != "" {
		if err := s.setSlackReaction(false, o.channel, o.messageTS, slackAckReaction); err != nil {
			slog.Debug("slack: clear ack reaction failed (non-fatal)", "component", "opencode", "err", err)
		}
	}
	if reply == "" {
		return
	}
	if err := s.postSlackMessage(o.channel, o.threadTS, reply); err != nil {
		slog.Error("slack reply send failed", "component", "opencode", "channel", o.channel, "error", err)
		return
	}
	slog.Info("slack reply posted", "component", "opencode", "channel", o.channel, "threaded", o.threadTS != "")
}
