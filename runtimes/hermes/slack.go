package hermes

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// slackOrigin records where an inbound Slack turn came from so the reply can be finalized in the same channel/thread and the ack reaction cleared.
type slackOrigin struct {
	channel   string
	threadTS  string
	messageTS string // the user message ts — used to add/remove the eyes reaction
}

// slackEnvelope is the outer Slack Events API shape the bff proxy forwards verbatim.
type slackEnvelope struct {
	Type      string     `json:"type"`      // "url_verification" | "event_callback"
	Challenge string     `json:"challenge"` // url_verification only
	TeamID    string     `json:"team_id"`   // event_callback — required by chat.startStream for channels
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
	channel   string
	threadTS  string
	messageTS string
	teamID    string
}

// parseSlackInbound decodes a forwarded Slack event into a challenge, a message, or neither (ignored).
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
	return "", &parsedSlackMsg{text: text, channel: ev.Channel, threadTS: ev.ThreadTS, messageTS: ev.TS, teamID: env.TeamID}, nil
}

// HandleInboundSlack implements domain.SlackBridge.
func (s *HermesService) HandleInboundSlack(in domain.SlackInbound) (string, bool, error) {
	challenge, msg, err := parseSlackInbound(in.Body, s.config.SlackUserID)
	if err != nil {
		return "", false, err
	}
	if challenge != "" {
		slog.Info("slack: url_verification challenge", "component", "hermes")
		return challenge, false, nil
	}
	if msg == nil {
		return "", false, nil
	}

	reqID, runID := s.NextChatRunID()

	threadTS := msg.threadTS
	if threadTS == "" {
		threadTS = msg.messageTS
	}
	s.markSlackOrigin(runID, msg.channel, threadTS, msg.messageTS)

	go func(ch, tt string) {
		if err := s.setSlackAssistantStatus(ch, tt, slackTypingStatus); err != nil {
			slog.Debug("slack: setStatus failed (non-fatal — needs assistant:write)", "component", "hermes", "err", err)
		}
	}(msg.channel, threadTS)

	// Register the stream. chat.startStream opens lazily on the first content chunk (seeded with that text), so the bubble is never empty.
	s.startSlackStreamSession(runID, msg.channel, threadTS, msg.teamID)

	if _, err := s.SendChatMessageWithRun(msg.text, reqID, runID); err != nil {
		s.consumeSlackOrigin(runID)
		s.finishSlackStream(runID, "")
		_ = s.setSlackAssistantStatus(msg.channel, threadTS, "")
		return "", false, fmt.Errorf("send slack turn to hermes: %w", err)
	}
	// Acknowledge receipt with an eyes reaction (best-effort, like openclaw's slack plugin); DeliverSlackReply clears it when the reply lands.
	if msg.messageTS != "" {
		go func(channel, ts string) {
			if err := s.setSlackReaction(true, channel, ts, slackAckReaction); err != nil {
				slog.Debug("slack: ack reaction failed (non-fatal)", "component", "hermes", "err", err)
			}
		}(msg.channel, msg.messageTS)
	}
	slog.Info("slack: forwarded inbound to hermes", "component", "hermes", "channel", msg.channel, "run_id", runID)
	return "", true, nil
}

// markSlackOrigin records the channel/thread/message a runID came from.
func (s *HermesService) markSlackOrigin(runID, channel, threadTS, messageTS string) {
	if runID == "" || channel == "" {
		return
	}
	s.slackRunOriginMu.Lock()
	s.slackRunOrigin[runID] = slackOrigin{channel: channel, threadTS: threadTS, messageTS: messageTS}
	s.slackRunOriginMu.Unlock()
}

// IsSlackOriginRun peeks (non-consuming) whether runID is a Slack turn.
func (s *HermesService) IsSlackOriginRun(runID string) bool {
	if runID == "" {
		return false
	}
	s.slackRunOriginMu.Lock()
	_, ok := s.slackRunOrigin[runID]
	s.slackRunOriginMu.Unlock()
	return ok
}

// consumeSlackOrigin returns + clears the origin for runID.
func (s *HermesService) consumeSlackOrigin(runID string) (slackOrigin, bool) {
	s.slackRunOriginMu.Lock()
	defer s.slackRunOriginMu.Unlock()
	o, ok := s.slackRunOrigin[runID]
	if ok {
		delete(s.slackRunOrigin, runID)
	}
	return o, ok
}

// DeliverSlackReply implements domain.SlackBridge — finalizes the Slack turn.
func (s *HermesService) DeliverSlackReply(runID, text string) error {
	o, ok := s.consumeSlackOrigin(runID)
	if !ok {
		return nil
	}
	_ = s.setSlackAssistantStatus(o.channel, o.threadTS, "")
	if o.messageTS != "" {
		if err := s.setSlackReaction(false, o.channel, o.messageTS, slackAckReaction); err != nil {
			slog.Debug("slack: clear ack reaction failed (non-fatal)", "component", "hermes", "err", err)
		}
	}
	streamed := s.finishSlackStream(runID, text)
	if streamed {
		slog.Info("slack reply delivered (streamed)", "component", "hermes", "channel", o.channel)
		return nil
	}
	// Stream never opened (no content reached it, or startStream kept failing) — post the reply as a single message so it isn't lost.
	if text == "" {
		return nil
	}
	return s.PostSlackReply(o.channel, o.threadTS, text)
}
