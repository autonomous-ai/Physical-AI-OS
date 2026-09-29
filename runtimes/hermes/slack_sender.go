package hermes

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"
)

const (
	slackPostMessageURL    = "https://slack.com/api/chat.postMessage"
	slackStartStreamURL    = "https://slack.com/api/chat.startStream"
	slackAppendStreamURL   = "https://slack.com/api/chat.appendStream"
	slackStopStreamURL     = "https://slack.com/api/chat.stopStream"
	slackSetStatusURL      = "https://slack.com/api/assistant.threads.setStatus"
	slackReactionAddURL    = "https://slack.com/api/reactions.add"
	slackReactionRemoveURL = "https://slack.com/api/reactions.remove"
)

// slackAckReaction is the emoji added to an inbound message while the turn runs and removed when the reply lands (mirrors openclaw's "👀 while thinking").
const slackAckReaction = "eyes"

// slackTypingStatus is the assistant-thread status shown while the agent thinks (the "…is typing" affordance, matching openclaw).
const slackTypingStatus = "...is typing"

// slackAPIResponse is the subset of a Slack Web API reply we inspect. ts/channel are populated by chat.postMessage so the caller can later edit/delete the message.
type slackAPIResponse struct {
	OK      bool   `json:"ok"`
	Error   string `json:"error"`
	TS      string `json:"ts"`
	Channel string `json:"channel"`
}

// slackAPI performs a JSON POST to a Slack Web API method.
func (s *HermesService) slackAPI(url string, payload map[string]any, benignErrors ...string) (slackAPIResponse, error) {
	var out slackAPIResponse
	if s.config.SlackBotToken == "" {
		return out, fmt.Errorf("slack bot token not configured")
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return out, fmt.Errorf("marshal slack payload: %w", err)
	}
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return out, fmt.Errorf("build slack request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+s.config.SlackBotToken)
	req.Header.Set("Content-Type", "application/json; charset=utf-8")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return out, fmt.Errorf("slack request: %w", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if uerr := json.Unmarshal(raw, &out); uerr != nil {
		return out, fmt.Errorf("parse slack response (status %d): %w", resp.StatusCode, uerr)
	}
	if !out.OK {
		for _, b := range benignErrors {
			if out.Error == b {
				return out, nil
			}
		}
		return out, fmt.Errorf("slack %s failed: %s", url, out.Error)
	}
	return out, nil
}

// postSlackMessage posts text to a channel (threaded when threadTS != "") and returns the new message ts.
func (s *HermesService) postSlackMessage(channel, threadTS, text string) (string, error) {
	payload := map[string]any{"channel": channel, "text": text}
	if threadTS != "" {
		payload["thread_ts"] = threadTS
	}
	r, err := s.slackAPI(slackPostMessageURL, payload)
	return r.TS, err
}

// setSlackAssistantStatus sets (or, with status="", clears) the assistant-thread status — the native "…is typing" / "Generating response…" indicator.
func (s *HermesService) setSlackAssistantStatus(channel, threadTS, status string) error {
	_, err := s.slackAPI(slackSetStatusURL, map[string]any{
		"channel_id": channel,
		"thread_ts":  threadTS,
		"status":     status,
	})
	return err
}

// startSlackStream opens a native streaming message (chat.startStream) seeded with the first reply text (markdown_text chunk) so the bubble is never empty.
func (s *HermesService) startSlackStream(channel, threadTS, teamID, markdownText string) (string, error) {
	payload := map[string]any{
		"channel":   channel,
		"thread_ts": threadTS,
		"chunks": []map[string]any{
			{"type": "markdown_text", "markdown_text": markdownText},
		},
	}
	if teamID != "" {
		payload["recipient_team_id"] = teamID
	}
	r, err := s.slackAPI(slackStartStreamURL, payload)
	return r.TS, err
}

// appendSlackStream appends a reply text chunk to a live stream (chat.appendStream).
func (s *HermesService) appendSlackStream(channel, ts, markdownText string) error {
	_, err := s.slackAPI(slackAppendStreamURL, map[string]any{
		"channel": channel,
		"ts":      ts,
		"chunks": []map[string]any{
			{"type": "markdown_text", "markdown_text": markdownText},
		},
	})
	return err
}

// stopSlackStream finalizes a live stream (chat.stopStream) — clears the typing indicator and marks the message complete.
func (s *HermesService) stopSlackStream(channel, ts string) error {
	_, err := s.slackAPI(slackStopStreamURL, map[string]any{"channel": channel, "ts": ts})
	return err
}

// setSlackReaction adds (add=true) or removes a reaction emoji on a message.
func (s *HermesService) setSlackReaction(add bool, channel, ts, name string) error {
	if channel == "" || ts == "" {
		return nil
	}
	url := slackReactionRemoveURL
	if add {
		url = slackReactionAddURL
	}
	_, err := s.slackAPI(url, map[string]any{"channel": channel, "timestamp": ts, "name": name}, "already_reacted", "no_reaction")
	return err
}

// PostSlackReply posts an agent reply back to the originating Slack channel/thread.
func (s *HermesService) PostSlackReply(channel, threadTS, text string) error {
	if _, err := s.postSlackMessage(channel, threadTS, text); err != nil {
		return err
	}
	slog.Info("slack reply posted", "component", "hermes", "channel", channel, "threaded", threadTS != "")
	return nil
}

// SlackSender delivers proactive (sensing/broadcast) messages to Slack, mirroring TelegramSender.
type SlackSender struct {
	svc *HermesService
}

func (t *SlackSender) Name() string { return "slack" }

func (t *SlackSender) IsConfigured() bool {
	return t.svc.config.SlackBotToken != "" && t.svc.config.SlackUserID != ""
}

func (t *SlackSender) Send(msg string, _ string) error {
	_, err := t.svc.postSlackMessage(t.svc.config.SlackUserID, "", msg)
	return err
}
