package http

import (
	"encoding/json"
	"log/slog"
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

var emotionRe = regexp.MustCompile(`(?:\\"|")emotion(?:\\"|")\s*:\s*(?:\\"|")([a-zA-Z_]+)(?:\\"|")`)

// parseEmotion extracts the emotion name from tool-call args (plain or escaped JSON).
func parseEmotion(toolArgs string) string {
	if m := emotionRe.FindStringSubmatch(toolArgs); len(m) == 2 {
		return m[1]
	}
	return ""
}

// extractTTSText parses the text argument of a built-in tts tool call.
// Example: `{"text":"hello"}` or `hello` → "hello".
func extractTTSText(toolArgs string) string {
	var obj struct {
		Text string `json:"text"`
	}
	if json.Unmarshal([]byte(toolArgs), &obj) == nil && obj.Text != "" {
		return obj.Text
	}
	return strings.TrimSpace(toolArgs)
}

// isAgentNoReply reports whether text is a silent sentinel ("NO_REPLY", "NO_*" or bare "NO").
func isAgentNoReply(text string) bool {
	t := strings.TrimSpace(strings.ToUpper(text))
	if t == "NO" {
		slog.Warn("agent emitted bare NO instead of NO_REPLY — suppressing TTS", "component", "agent", "raw", text)
		return true
	}
	if strings.HasPrefix(t, "NO_") {
		slog.Warn("agent no-reply sentinel — suppressing TTS", "component", "agent", "raw", text)
		return true
	}
	return false
}

// metaNonReplyRe matches prose narrating a decision to stay silent (e.g. "Nothing to say").
var metaNonReplyRe = regexp.MustCompile(`(?i)\b(nothing (to|worth) (say|saying|add|adding|mention|mentioning|report|reporting|note|noting|comment|commenting)|no (user )?(message|reply|response|comment)( needed| required| necessary)?|no need to (reply|respond|speak|say)|not worth (saying|mentioning|a reply|a response)|staying silent|remaining silent|no action needed)\b`)

// metaNonReplyMaxLen limits the meta-non-reply gate to short lines to avoid false positives.
const metaNonReplyMaxLen = 100

// isMetaNonReply reports whether text is a short silent-decision narration (never a question).
func isMetaNonReply(text string) bool {
	t := strings.TrimSpace(text)
	if t == "" || len(t) > metaNonReplyMaxLen || strings.Contains(t, "?") {
		return false
	}
	if !metaNonReplyRe.MatchString(t) {
		return false
	}
	slog.Warn("agent narrated its silence instead of NO_REPLY — suppressing TTS",
		"component", "agent", "raw", t)
	return true
}

// sanitizeAgentText strips trailing internal sentinels. Example: "Hello! NO_REPLY" → "Hello!".
func sanitizeAgentText(text string) string {
	for _, sentinel := range []string{"NO_REPLY", "HEARTBEAT_OK"} {
		if idx := strings.LastIndex(strings.ToUpper(text), sentinel); idx >= 0 {
			cleaned := strings.TrimRight(text[:idx], " \t\n!.,—–-")
			if cleaned != "" {
				slog.Warn("stripped trailing sentinel from agent text", "component", "agent", "sentinel", sentinel, "before", text[:min(len(text), 100)], "after", cleaned)
				text = cleaned
			}
		}
	}
	return text
}

// sayTagRe captures the content of the first <say>...</say> pair (multi-line).
var sayTagRe = regexp.MustCompile(`(?s)<say>(.*?)</say>`)

// extractSayTag returns the <say> content, text unchanged if no tag, or NO_REPLY for an empty tag.
func extractSayTag(text string) string {
	m := sayTagRe.FindStringSubmatch(text)
	if m == nil {
		return text
	}
	inner := strings.TrimSpace(m[1])
	if inner == "" {
		slog.Info("empty <say> tag — treating as NO_REPLY", "component", "agent")
		return "NO_REPLY"
	}
	slog.Info("extracted <say> tag", "component", "agent", "before_len", len(text), "after", inner[:min(len(inner), 100)])
	return inner
}

// isDeviceOutboundChatRunID reports whether runID is a device chat.send key (device-chat-* / device-sensing-*).
func isDeviceOutboundChatRunID(runID string) bool {
	if runID == "" {
		return false
	}
	return strings.HasPrefix(runID, "device-chat-") || strings.HasPrefix(runID, "device-sensing-")
}

// labelForDeviceInternal returns the Flow Monitor label for a device-internal message,
// or "" if text has no known internal prefix.
func labelForDeviceInternal(text string) string {
	switch {
	case strings.HasPrefix(text, "[user] [ambient]"),
		strings.HasPrefix(text, "[ambient]"),
		strings.HasPrefix(text, "[user]"):
		return "[voice]"
	case strings.HasPrefix(text, "[emotion]"):
		return "[emotion]"
	case strings.HasPrefix(text, "[speech_emotion]"):
		return "[speech_emotion]"
	case strings.HasPrefix(text, "[activity]"):
		return "[activity]"
	case strings.HasPrefix(text, "[wellbeing]"):
		return "[wellbeing]"
	case strings.HasPrefix(text, "[music-proactive]"):
		return "[music]"
	case strings.HasPrefix(text, "[system]"):
		return "[system]"
	case strings.HasPrefix(text, "[sensing:"):
		return "[sensing]"
	}
	return ""
}

// isChannelOriginatedRun reports whether any runID came from a real channel user ("tg-<msgID>").
func isChannelOriginatedRun(runIDs ...string) bool {
	for _, r := range runIDs {
		if strings.HasPrefix(r, "tg-") {
			return true
		}
	}
	return false
}

// canStreamSentenceTTS reports whether the run may stream its first sentence to the speaker
// (not channel, web chat, silent, Slack, or TTS-suppressed).
func (h *AgentHandler) canStreamSentenceTTS(runID, flowRunID string) bool {
	if isChannelOriginatedRun(runID, flowRunID) {
		return false
	}
	if h.agentGateway.IsWebChatRun(flowRunID) {
		return false
	}
	// Realtime agent already spoke; IsSilentRun is non-consuming so lifecycle:end's
	// ConsumeSilentRun still works.
	if h.agentGateway.IsSilentRun(flowRunID) || h.agentGateway.IsSilentRun(runID) {
		return false
	}
	// Slack turns reply in Slack, never on the speaker.
	if sb, ok := h.agentGateway.(domain.SlackBridge); ok && (sb.IsSlackOriginRun(runID) || sb.IsSlackOriginRun(flowRunID)) {
		return false
	}
	h.channelRunsMu.Lock()
	if h.channelRuns[runID] || h.channelRuns[flowRunID] {
		h.channelRunsMu.Unlock()
		return false
	}
	h.channelRunsMu.Unlock()
	h.ttsSuppressMu.Lock()
	_, suppressed := h.ttsSuppressReasons[runID]
	h.ttsSuppressMu.Unlock()
	return !suppressed
}

// extractMessageContentText joins text from a session.message `content` (string or
// typed-block array; only "text" blocks count).
func extractMessageContentText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return ""
	}
	parts := make([]string, 0, len(blocks))
	for _, b := range blocks {
		if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "")
}

// deviceInternalPrefixes mark chat.sends issued by the device itself; such text must
// never mark a run as a channel turn (backs up the bounded IsRecentOutboundChat buffer).
var deviceInternalPrefixes = []string{
	"[sensing:",
	"[ambient]",
	"[activity]",
	"[emotion]",
	"[speech_emotion]",
	"[wellbeing]",
	"[music-proactive]",
	"[system]",
	"You just woke up",
	"Bạn vừa thức dậy",
	"你刚刚醒来",
	"你剛剛醒來",
}

// isDeviceInternalMessage reports whether text starts with a device-internal prefix.
func isDeviceInternalMessage(text string) bool {
	if text == "" {
		return false
	}
	for _, p := range deviceInternalPrefixes {
		if strings.HasPrefix(text, p) {
			return true
		}
	}
	return false
}

// telegramChatIDRe matches queue-mode metadata, e.g. `"chat_id": "telegram:158406741"`.
var telegramChatIDRe = regexp.MustCompile(`"chat_id"\s*:\s*"telegram:(\d+)"`)

// extractTelegramChatID returns the numeric Telegram chat_id from a message body, or "".
func extractTelegramChatID(text string) string {
	if m := telegramChatIDRe.FindStringSubmatch(text); len(m) == 2 {
		return m[1]
	}
	return ""
}

// senderLabelTelegramIDRe captures the id from senderLabels like "Leo (@x) id:158406741" or "Leo (158406741)".
var senderLabelTelegramIDRe = regexp.MustCompile(`(?:id:|\()(\d{6,})\)?`)

// extractTelegramIDFromSenderLabel returns the numeric Telegram user ID in label, or "".
func extractTelegramIDFromSenderLabel(label string) string {
	if label == "" {
		return ""
	}
	if m := senderLabelTelegramIDRe.FindStringSubmatch(label); len(m) == 2 {
		return m[1]
	}
	return ""
}

// shortError shortens an error string, reducing HTML error pages to their status line.
func shortError(errMsg string) string {
	if idx := strings.Index(errMsg, "<!"); idx > 0 {
		prefix := strings.TrimSpace(errMsg[:idx])
		if i := strings.Index(errMsg, "unable_to_access"); i > 0 {
			if j := strings.Index(errMsg[i:], ">"); j > 0 {
				if k := strings.Index(errMsg[i+j:], "<"); k > 0 {
					domain := strings.TrimSpace(errMsg[i+j+1 : i+j+k])
					if domain != "" {
						return prefix + " blocked by Cloudflare (" + domain + ")"
					}
				}
			}
		}
		return prefix + " (HTML error page)"
	}
	if len(errMsg) > 120 {
		return errMsg[:120] + "..."
	}
	return errMsg
}
