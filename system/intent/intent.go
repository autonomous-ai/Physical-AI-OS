// Package intent provides local intent matching for common voice commands.
// Matched commands execute directly against HAL APIs, bypassing OpenClaw
// for instant response (~50ms vs ~3-5s through the agent pipeline).
//
// Layout (one file per function group, all one package):
//   - intent.go         — engine: Result, Match, capability gate, shared helpers
//   - chitchat.go       — exact-match greetings/farewells/thanks (vi/en/zh)
//   - rules_led.go      — LED color / on / off / dim
//   - rules_scene.go    — scene activation + scene off
//   - rules_audio.go    — volume, mute, music stop, TTS stop
//   - rules_misc.go     — what-time and other hardware-free rules
//   - rules_tracking.go — servo vision tracking
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
	// Source is "jev" for an optional semantic fallback; empty means local rules.
	Source string
	// ExecutionFailed records an error from any attempted HAL action.
	ExecutionFailed bool
	// TTSText is spoken back to the user via /voice/speak.
	TTSText string
	// LEDChanged is true when this intent sets an LED color/scene (locks ambient breathing).
	LEDChanged bool
	// LEDOff is true when this intent turns the LED off (unlocks ambient breathing).
	LEDOff bool
	// Emotion is the emotion name if this intent triggered an /emotion call.
	Emotion string
	// Rule is the name of the matched rule for debugging.
	Rule string
	// Actions lists hardware API calls made during exec (e.g. "POST /led/solid", "POST /emotion").
	Actions []string
}

type rule struct {
	name  string
	match func(string) bool
	exec  func(string) *Result
	// capability gates the rule to devices that declare it in ROBOT.md (e.g.
	// "motion" for servo tracking). Empty = no hardware dependency, always on.
	capability string
}

// Checked in order — first match wins. Grouped by function into rules_*.go;
// ordering constraints (led_color before generic led on/off, scene_off before
// scene activation) live inside each group, and the group order below
// preserves the original single-table order.
var rules = slices.Concat(ledRules, sceneRules, audioRules, miscRules, trackingRules)

// Match tries to match a voice command to a local intent. Returns nil if no
// match — caller should fall through to OpenClaw. Chitchat (exact-match
// greetings/farewells/thanks across vi/en/zh) is checked first so a bare
// "chào" / "hi" / "你好" hits the WAV cache in ~50ms instead of the 8s LLM TTFT.
//
// Rules that drive a peripheral are gated by the device's declared capabilities
// (set once via Configure): a body without that hardware (e.g. intern-v2 has no
// servo) never matches a command it can't execute, and never POSTs to a HAL
// route its body doesn't serve.
func Match(text string) *Result {
	return match(text, chitchatEnabled())
}

// MatchCommands is Match with chitchat skipped — only the command rules run.
// Use it when something else already owns social talk (see SetChitchatEnabled).
func MatchCommands(text string) *Result {
	return match(text, false)
}

func match(text string, allowChitchat bool) *Result {
	// Chitchat needs a stricter normalization than command rules — speaker
	// prefixes, voice tags, and the (audio saved at ...) suffix from the
	// sensing message must be stripped for an exact phrase match to work.
	if allowChitchat {
		if r := matchChitchat(stripChitchatPrefixes(text)); r != nil {
			return r
		}
	}

	return recognizeCommand(text).execute()
}

// command separates recognition from side effects. Only code-owned rules and
// their validated input reach execution; a model never supplies a HAL payload.
type command struct {
	rule *rule
	text string
}

func (c *command) execute() *Result {
	if c == nil || !capEnabled(c.rule.capability) {
		return nil
	}
	// HAL silently drops solid LED writes during sleep. Check before writing
	// so a chat command cannot report success or wake the device as a side effect.
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
		// Retain specific failure explanations from stateful executors.
		if !strings.HasPrefix(result.TTSText, "I couldn't") {
			result.TTSText = "I couldn't complete that action. Please try again."
		}
	}
	return result
}

func recognizeCommand(text string) *command {
	// Field order beats rule order: a rule matching the agent's summary wins
	// over a different rule matching the noisy transcript. Within one field
	// the table order is unchanged.
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

// deviceCaps is the device's declared capability set (ROBOT.md), set once at
// startup via Configure and read-only after. It gates which command rules run
// and which HAL peripherals local intents drive. nil = fail-open (all rules),
// matching legacy single-device behavior.
var deviceCaps map[string]bool

// Configure sets the capability set used to gate local intents. Call once at
// startup before any Match. nil/empty caps = fail-open.
func Configure(caps map[string]bool) { deviceCaps = caps }

// chitchatOff turns off the social rules while another layer owns social talk.
// Guarded by chitchatMu because the config watcher flips it at runtime.
var (
	chitchatMu  sync.RWMutex
	chitchatOff bool
)

// SetChitchatEnabled turns the chitchat rules on or off.
//
// They exist to answer "hi" / "chào" from the WAV cache instead of paying an
// LLM round-trip. The realtime voice agent answers social talk itself, in
// under a second and in character, and it gets every voice turn BEFORE
// os-server does — so with realtime on, chitchat only ever fires on turns the
// model stayed silent for, where it barges in with a canned line in a
// different voice. Off is the right default there; the command rules (lights,
// volume, time) stay on either way since those genuinely beat the model.
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

// capEnabled is fail-open: an empty capability (no hardware dependency) is always
// on; otherwise nil/empty deviceCaps → true. The maximal reference device (Lamp)
// declares every capability, so it keeps every rule.
func capEnabled(capability string) bool {
	if capability == "" {
		return true
	}
	return len(deviceCaps) == 0 || deviceCaps[capability]
}

// CacheableReplies is the set of intent reply phrases that should be
// pre-rendered into the hal WAV cache at boot. Listed here (and not
// derived from the rules table) because rule.exec is dynamic — some
// replies depend on runtime input (color name, current time) and aren't
// suitable for caching.
var CacheableReplies = func() []string {
	out := []string{
		"Light on!", "Light off!", "Back to normal!", "Goodnight!",
		"Volume up!", "Volume down!", "Music stopped.", "Dimmed.", "Max brightness!",
		"Speaker on!",
	}
	// Pull every chitchat reply variant from i18n so the WAV cache covers
	// them after reboot — first call is then ~50ms playback instead of 1.5s
	// ElevenLabs render.
	for _, r := range chitchatRules {
		out = append(out, i18n.AllVariantsAcrossLangs(r.reply)...)
	}
	return out
}()

// --- shared helpers ---

func normalize(s string) string {
	return strings.ToLower(strings.TrimSpace(s))
}

// Envelope markers HAL wraps around a delegated voice turn
// (hal/drivers/voice/_internal/turn_dispatch.py:261).
const (
	instructionMarker = "[voice-instruction]"
	transcriptMarker  = "[transcript]"
)

// Routing metadata that rides along with the turn but is nobody's speech. It
// must be removed before matching: a snapshot path can contain a target noun as
// a whole word — "/var/lib/hal/snapshots/sensing_face/1788839050669.jpg" holds
// "face", which is a tracking target, between two non-word characters.
// os-server strips these downstream (handler.go:689) but only well after the
// intent match at handler.go:215.
var (
	reSnapshotTag = regexp.MustCompile(`\[snapshot:[^\]]*\]`)
	reVisionHint  = regexp.MustCompile(`\[vision-image\][^\n]*`)
)

// voiceFields returns the texts the command rules should try, most
// authoritative first.
//
// HAL emits three shapes, and which one arrives depends on the turn's ROUTE,
// not on the conversation (turn_dispatch.py:260):
//
//	ROUTE_DELEGATED, model supplied a message -> "[voice-instruction] …\n[transcript] …"
//	ROUTE_DELEGATED, no message               -> the bare transcript
//	realtime_not_started / _unavailable /
//	_no_output / _error                       -> "Unknown Speaker: [voice:x] … (audio saved at …)"
//
// So the preamble appears and disappears BETWEEN TURNS of one conversation:
// green-lamp 2026-09-08, the first voice turn had none and the next five did.
// The same sentence must therefore resolve the same way in all three shapes.
//
// The preamble goes first because skills/input-branching/SKILL.md makes it the
// primary input — STT is locked to one language while the user may speak
// another, so the transcript can be gibberish while the summary is clean. They
// are returned SEPARATELY and never concatenated: matching one blob let a rule
// take its verb from the summary and its target from the transcript, which is
// how the 10:46:19 turn fired a command the user never gave.
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

// narrationPronouns are the words a THIRD-PERSON summary uses that a rule must
// not read as a target. The preamble talks ABOUT the user ("User wants lamp to
// track their typing"), so "user" is a grammatical subject, not something to
// point a camera at — and "me" in a summary means the LAMP, not the speaker:
// green-lamp 2026-09-08 10:47:58 emitted "User wants to connect me with their
// clock". Blanked only in the preamble; the transcript keeps its pronouns,
// where "follow me" really does mean the person talking.
//
// Only the tracking rule reads these words today, but the fact is about the
// field, not the rule, so it belongs here.
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

// containsPhrase reports whether kw occurs in t as a whole phrase: the
// occurrence must not sit inside a larger word, so "unmute speaker" does
// NOT match keyword "mute speaker". Boundaries are non-alphanumeric ASCII;
// multibyte (Vietnamese/Chinese) neighbors count as boundaries, which is
// correct since keywords are English-only.
func containsPhrase(t, kw string) bool { return indexPhrase(t, kw) >= 0 }

// indexPhrase is containsPhrase with the position: it returns the index of the
// first whole-phrase occurrence of kw in t, or -1. Tracking target selection
// needs the position to tell an object named after the verb ("track my
// keyboard") from a pronoun that merely appears somewhere in the sentence.
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

// pickRandom returns a pseudo-random pick using the current time. Avoids
// pulling in math/rand state for low-stakes variance.
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

// postEmotion drives an emotion expression, but only on a body that can show one
// (declares the `expression` capability — a screen, LED, or servo to express
// through). Emotion is cross-cutting (fired as a flourish by several command/
// chitchat rules), so the guard lives here rather than on each rule's capability.
// Fail-open via capEnabled.
func postEmotion(body string) error {
	if capEnabled(device.CapExpression) {
		return post("/emotion", body)
	}
	return nil
}
