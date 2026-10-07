package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/lib/i18n"
)

// chitchatRule is one chitchat intent; phrases and replies live in i18n.
type chitchatRule struct {
	reply   i18n.Phrase // i18n key — input matchers + reply variants both keyed by this
	intent  string      // "greeting" / "farewell" / "thanks" — for log/Rule field
	emotion string      // emotion fired alongside reply
}

// Order matters: specific intents before broad ones; nevermind last (short trigger words).
var chitchatRules = []chitchatRule{
	{reply: i18n.PhraseChitchatPresenceCheck, intent: "presence_check", emotion: "happy"},
	{reply: i18n.PhraseChitchatApology, intent: "apology", emotion: "happy"},
	{reply: i18n.PhraseChitchatCompliment, intent: "compliment", emotion: "happy"},
	{reply: i18n.PhraseChitchatGreeting, intent: "greeting", emotion: "happy"},
	{reply: i18n.PhraseChitchatFarewell, intent: "farewell", emotion: "happy"},
	{reply: i18n.PhraseChitchatThanks, intent: "thanks", emotion: "happy"},
	{reply: i18n.PhraseChitchatNevermind, intent: "nevermind", emotion: "idle"},
}

// matchChitchat returns a Result for a short social phrase (<=5 words, no command verbs),
// replying in the language of the matched input.
func matchChitchat(text string) *Result {
	if text == "" {
		return nil
	}
	t := strings.ToLower(strings.TrimSpace(text))
	t = strings.TrimRight(t, ".!?,。！？，")

	// A bare wake word means the user is just calling the device.
	t = stripWakeWord(t)
	if t == "" {
		return bareAttentionResult()
	}

	// Longer utterances go to the LLM; CJK counts runes as words.
	if wordCountLoose(t) > 5 {
		return nil
	}

	for _, w := range i18n.ChitchatCommandWords() {
		if strings.Contains(t, w) {
			return nil
		}
	}

	for _, r := range chitchatRules {
		for lang, phrases := range i18n.InputPhrases(r.reply) {
			for _, p := range phrases {
				// Whole-phrase match: "hi" must not match inside "this".
				matches := containsPhrase(t, p)
				// Japanese has no word separators: a social phrase inside a
				// question must fall through to the agent (e.g. 何している？).
				if lang == i18n.LangJA {
					matches = t == strings.TrimRight(p, ".!?,。！？，")
				}
				if !matches {
					continue
				}
				reply := i18n.PickIn(r.reply, lang)
				if reply == "" {
					continue
				}
				executionFailed := postEmotion(fmt.Sprintf(`{"emotion":"%s","intensity":0.7}`, r.emotion)) != nil
				return &Result{
					ExecutionFailed: executionFailed,
					TTSText:         reply,
					Emotion:         r.emotion,
					Rule:            "chitchat_" + r.intent,
					Actions:         []string{"POST /emotion " + r.emotion},
				}
			}
		}
	}
	return nil
}

// wordCountLoose counts space-separated tokens, or runes/2 for CJK text.
func wordCountLoose(s string) int {
	fields := strings.Fields(s)
	if len(fields) > 1 {
		return len(fields)
	}
	for _, r := range s {
		if r > 127 {
			n := 0
			for range s {
				n++
			}
			if n/2 < 1 {
				return 1
			}
			return n / 2
		}
	}
	return len(fields)
}

// stripChitchatPrefixes removes the sensing envelope around the user's words.
// Example: "[user] Unknown Speaker: [voice:v1] chào (audio saved at /tmp/x)" -> "chào"
func stripChitchatPrefixes(s string) string {
	s = strings.TrimSpace(s)
	for strings.HasPrefix(s, "[") {
		end := strings.Index(s, "]")
		if end < 0 {
			break
		}
		s = strings.TrimSpace(s[end+1:])
	}
	// Only strip a speaker label when the colon is near the start.
	if idx := strings.Index(s, ":"); idx >= 0 && idx < 40 {
		before := strings.ToLower(s[:idx])
		if strings.Contains(before, "speaker") {
			s = strings.TrimSpace(s[idx+1:])
		}
	}
	for strings.HasPrefix(s, "[") {
		end := strings.Index(s, "]")
		if end < 0 {
			break
		}
		s = strings.TrimSpace(s[end+1:])
	}
	// Strip only our own "(audio ...)" annotation; other parentheses stay.
	if idx := strings.LastIndex(s, "("); idx > 0 {
		rest := s[idx:]
		if strings.Contains(rest, "audio saved") || strings.Contains(rest, "audio is too short") {
			s = strings.TrimSpace(s[:idx])
		}
	}
	return s
}

// stripWakeWord removes a leading wake-word token at a word boundary (longest form first).
func stripWakeWord(s string) string {
	for _, w := range i18n.ChitchatWakeWords() {
		if !strings.HasPrefix(s, w) {
			continue
		}
		rest := s[len(w):]
		if rest == "" {
			return ""
		}
		c := rest[0]
		if c == ' ' || c == ',' || c == '.' || c == '!' || c == '?' {
			return strings.TrimSpace(strings.TrimLeft(rest, " ,.!?"))
		}
	}
	return s
}

// bareAttentionResult greets the user who said only the wake word.
func bareAttentionResult() *Result {
	reply := i18n.Pick(i18n.PhraseChitchatGreeting)
	if reply == "" {
		return nil
	}
	executionFailed := postEmotion(`{"emotion":"happy","intensity":0.7}`) != nil
	return &Result{
		ExecutionFailed: executionFailed,
		TTSText:         reply,
		Emotion:         "happy",
		Rule:            "chitchat_attention",
		Actions:         []string{"POST /emotion happy"},
	}
}
