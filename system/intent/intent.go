// Package intent matches common voice commands locally and executes them against
// HAL directly, bypassing the agent for a fast reply.
package intent

import (
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// Result holds what to do after a match: the HAL action + a TTS reply.
type Result struct {
	// Source is "jev" for the semantic fallback; empty means local rules.
	Source string
	// ExecutionFailed records an error from any attempted HAL action.
	ExecutionFailed bool
	// TTSText is spoken back via /voice/speak.
	TTSText string
	// LEDChanged is true when the intent sets an LED color/scene (locks ambient breathing).
	LEDChanged bool
	// LEDOff is true when the intent turns the LED off (unlocks ambient breathing).
	LEDOff bool
	// Emotion is the emotion name if this intent triggered an /emotion call.
	Emotion string
	// Rule is the matched rule name.
	Rule string
	// Actions lists HAL calls made, e.g. "POST /led/solid".
	Actions []string
}

type rule struct {
	name  string
	match func(string) bool
	exec  func(string) *Result
	// capability gates the rule on a ROBOT.md capability; empty means always on.
	capability string
}

// rules are checked in order; first match wins (ordering constraints live in each group).
var rules = slices.Concat(ledRules, sceneRules, audioRules, miscRules, trackingRules)

// Match matches text to a local intent (chitchat first), or returns nil to fall through
// to the agent. Rules are gated by the device capabilities set via Configure.
func Match(text string) *Result {
	return match(text, chitchatEnabled())
}

// MatchCommands is Match without chitchat.
func MatchCommands(text string) *Result {
	return match(text, false)
}

func match(text string, allowChitchat bool) *Result {
	// Chitchat needs stricter normalization (speaker prefixes, voice tags, audio suffix).
	if allowChitchat {
		if r := matchChitchat(stripChitchatPrefixes(text)); r != nil {
			return r
		}
	}

	return recognizeCommand(text).execute()
}

// command pairs a code-owned rule with validated input; a model never supplies a HAL payload.
type command struct {
	rule *rule
	text string
}

func (c *command) execute() *Result {
	if c == nil || !capEnabled(c.rule.capability) {
		return nil
	}
	// HAL drops solid LED writes during sleep; check first so we don't report false success.
	if c.rule.name == "led_on" || c.rule.name == "led_color" || c.rule.name == "dim" {
		sleeping, err := hal.GetSleeping()
		if err != nil || sleeping {
			reply := "I couldn't check whether the light is available. Please try again."
			if sleeping {
				reply = "The device is asleep. Wake it before changing the light."
			}
			return &Result{Rule: c.rule.name, ExecutionFailed: true, TTSText: reply, Actions: []string{"GET /emotion/status"}}
		}
	}
	result := c.rule.exec(c.text)
	result.Rule = c.rule.name
	if result.ExecutionFailed {
		result.LEDChanged = false
		result.LEDOff = false
		result.Emotion = ""
		if !strings.HasPrefix(result.TTSText, "I couldn't") {
			result.TTSText = "I couldn't complete that action. Please try again."
		}
	}
	return result
}

func recognizeCommand(text string) *command {
	// Field order beats rule order: a summary match wins over a transcript match.
	for _, t := range voiceFields(normalize(text)) {
		for i := range rules {
			r := &rules[i]
			if !capEnabled(r.capability) {
				continue
			}
			if r.match(t) {
				return &command{rule: r, text: t}
			}
		}
	}
	return nil
}

// deviceCaps is the ROBOT.md capability set, set once via Configure; nil is fail-open.
var deviceCaps map[string]bool

// Configure sets the capability set gating local intents; call once before Match.
func Configure(caps map[string]bool) { deviceCaps = caps }

// chitchatOff disables social rules; guarded by chitchatMu (flipped at runtime).
var (
	chitchatMu  sync.RWMutex
	chitchatOff bool
)

// SetChitchatEnabled turns the chitchat rules on or off (off when realtime voice owns social talk).
func SetChitchatEnabled(enabled bool) {
	chitchatMu.Lock()
	chitchatOff = !enabled
	chitchatMu.Unlock()
}

func chitchatEnabled() bool {
	chitchatMu.RLock()
	defer chitchatMu.RUnlock()
	return !chitchatOff
}

// capEnabled reports whether a capability is enabled; fail-open on empty/nil caps.
func capEnabled(capability string) bool {
	if capability == "" {
		return true
	}
	return len(deviceCaps) == 0 || deviceCaps[capability]
}

// CacheableReplies are the static reply phrases pre-rendered into the HAL WAV cache.
var CacheableReplies = func() []string {
	out := []string{
		"Light on!", "Light off!", "Back to normal!", "Goodnight!",
		"Volume up!", "Volume down!", "Music stopped.", "Dimmed.", "Max brightness!",
		"Speaker on!",
	}
	for _, r := range chitchatRules {
		out = append(out, i18n.AllVariantsAcrossLangs(r.reply)...)
	}
	return out
}()

func normalize(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// Envelope markers HAL wraps around a delegated voice turn.
const (
	instructionMarker = "[voice-instruction]"
	transcriptMarker  = "[transcript]"
)

// Routing metadata stripped before matching (a snapshot path may contain a target noun).
var (
	reSnapshotTag = regexp.MustCompile(`\[snapshot:[^\]]*\]`)
	reVisionHint  = regexp.MustCompile(`\[vision-image\][^\n]*`)
)

// voiceFields returns the texts to match, most authoritative first (preamble, then transcript).
// Fields are never concatenated so one rule cannot mix verb and target across them.
func voiceFields(s string) []string {
	s = reSnapshotTag.ReplaceAllString(s, " ")
	s = reVisionHint.ReplaceAllString(s, " ")
	if i := strings.LastIndex(s, transcriptMarker); i >= 0 {
		pre := s[:i]
		if j := strings.Index(pre, instructionMarker); j >= 0 {
			pre = pre[j+len(instructionMarker):]
		}
		return nonEmptyFields(neutralisePronouns(pre),
			stripChitchatPrefixes(s[i+len(transcriptMarker):]))
	}
	return nonEmptyFields(stripChitchatPrefixes(s))
}

// narrationPronouns are blanked in the third-person preamble only ("me" there means the lamp).
var narrationPronouns = []string{"user", "users", "me", "myself", "us"}

func neutralisePronouns(s string) string {
	for _, w := range narrationPronouns {
		for {
			i := indexPhrase(s, w)
			if i < 0 {
				break
			}
			s = s[:i] + strings.Repeat(" ", len(w)) + s[i+len(w):]
		}
	}
	return s
}

func nonEmptyFields(vals ...string) []string {
	out := make([]string, 0, len(vals))
	for _, v := range vals {
		if v = strings.TrimSpace(v); v != "" {
			out = append(out, v)
		}
	}
	return out
}

func anyOf(keywords ...string) func(string) bool {
	return func(t string) bool {
		for _, kw := range keywords {
			if containsPhrase(t, kw) {
				return true
			}
		}
		return false
	}
}

// containsPhrase reports whether kw occurs in t as a whole phrase.
// Example: "unmute speaker" does not match "mute speaker".
func containsPhrase(t, kw string) bool { return indexPhrase(t, kw) >= 0 }

// indexPhrase returns the index of the first whole-phrase occurrence of kw in t, or -1.
func indexPhrase(t, kw string) int {
	for i := 0; ; {
		j := strings.Index(t[i:], kw)
		if j < 0 {
			return -1
		}
		start := i + j
		end := start + len(kw)
		if (start == 0 || !isASCIIWordChar(t[start-1])) &&
			(end == len(t) || !isASCIIWordChar(t[end])) {
			return start
		}
		i = start + 1
	}
}

func isASCIIWordChar(b byte) bool {
	return b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9'
}

// pickRandom returns a time-seeded pick from opts.
func pickRandom(opts []string) string {
	if len(opts) == 0 {
		return ""
	}
	return opts[int(time.Now().UnixNano())%len(opts)]
}

func post(path, body string) error {
	err := hal.PostRaw(path, body)
	if err != nil {
		slog.Warn("[intent] hal call failed", "path", path, "error", err)
	}
	return err
}

// postEmotion drives an emotion expression, only on bodies with the expression capability.
func postEmotion(body string) error {
	if capEnabled(device.CapExpression) {
		return post("/emotion", body)
	}
	return nil
}
