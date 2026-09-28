// Package sensingmsg builds the agent-bound message text for a sensing event (direct and replay paths).
package sensingmsg

import (
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/skillcontext"
)

// IsChat reports whether eventType is a typed-chat turn (web_chat or mqtt_chat).
func IsChat(eventType string) bool {
	return eventType == "web_chat" || eventType == "mqtt_chat"
}

// EnterNamesNewFriend reports whether a presence.enter text announces a newly visible friend.
// Mirrors HAL's enter_message.has_new_friend (only the `new:` segment counts).
func EnterNamesNewFriend(message string) bool {
	head, _, _ := strings.Cut(message, ";")
	return strings.Contains(strings.ToLower(head), "friend (")
}

// EnterHasPresentFriend reports whether a presence.enter text lists an already-present friend.
func EnterHasPresentFriend(message string) bool {
	return strings.Contains(message, "already present:")
}

// Build returns the agent message for a sensing event.
// currentUser "" means unknown; guardTag is the guard-mode prefix, or "" (always "" on the drain path).
func Build(eventType, message, currentUser, guardTag string) string {
	switch eventType {
	case "voice_command", "voice_followup":
		return domain.AppendEnrollNudge("[user] " + message)
	case "voice":
		// `[ambient]` drives voice/SKILL.md's overheard-audio mute guard.
		return domain.AppendEnrollNudge("[user] [ambient] " + message)
	case "web_chat", "mqtt_chat":
		// Slash commands skip `[user]` so the command router sees the leading slash.
		if strings.HasPrefix(message, "/") {
			return message
		}
		return "[user] " + message
	}

	if guardTag != "" && eventType != "environment.update" {
		return guardTag + " " + message
	}

	// Domain-specific prefixes route passive events to their dedicated skill.
	var msg string
	switch eventType {
	case "environment.update":
		msg = "[environment:update] " + message
	case "motion.activity":
		msg = "[activity] " + message
	case "emotion.detected":
		msg = "[emotion] " + message
	case "speech_emotion.detected":
		msg = "[speech_emotion] " + message
	default:
		msg = "[sensing:" + eventType + "] " + message
	}

	if currentUser == "" {
		currentUser = "unknown"
	}

	switch eventType {
	case "environment.update":
		msg += "\n[Use the environment skill and well-being guidance. Advisory sensor context, not a user request or a safety alarm. No mandatory speech or emotion; NO_REPLY when no useful, timely action is warranted.]"
	case "presence.enter":
		// Attribution must precede the presence digest or the agent greets by the stale USER.md name.
		msg += "\n[context: current_user=" + currentUser + "]"
		// Presence context only when the arrival itself is a friend; current_user may be a stale friend.
		if EnterNamesNewFriend(message) {
			msg += skillcontext.BuildPresenceContext(currentUser)
		} else if EnterHasPresentFriend(message) {
			// Inline rule: runtimes may skip loading sensing/SKILL.md.
			msg += "\n[A stranger joined " + currentUser + ", who is in frame — speak to " + currentUser + ", not to the stranger. See sensing/SKILL.md \"Someone joins the user\".]"
		}
	case "presence.leave", "presence.away":
		msg += "\n[No crons to cancel. NO_REPLY unless worth saying.]"
	case "touch.head_pat":
		// HAL already spoke a pet-response phrase locally.
		msg += "\n[NO_REPLY unless worth saying — phrase already spoken locally.]"
	case "motion.activity":
		// current_user goes right after the activity prefix line, before the payload.
		parts := strings.SplitN(msg, "\n", 2)
		head := parts[0]
		tail := ""
		if len(parts) > 1 {
			tail = "\n" + parts[1]
		}
		msg = head + "\n[context: current_user=" + currentUser + "]" + tail
		msg += skillcontext.BuildUserContext(currentUser)
		msg += skillcontext.BuildWellbeingContext(currentUser)
	case "emotion.detected", "speech_emotion.detected":
		msg += "\n[context: current_user=" + currentUser + "]"
		msg += skillcontext.BuildUserContext(currentUser)
		msg += skillcontext.BuildEmotionContext(skillcontext.ExtractDetectedEmotion(message), currentUser)
	}

	// Sensor events carry no user text, so inject the device locale.
	msg += i18n.LangContextTag()
	return msg
}
