package intent

import (
	"net/http"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
)

// Regression: "Deactivate focus mode" used to fall through scene_off (which
// only knew "turn off"/"disable") into scene_focus, re-activating the scene
// the user asked to leave.
func TestSceneOffPhrasings(t *testing.T) {
	for _, text := range []string{
		"Deactivate focus mode",
		"turn off focus mode",
		"disable the focus scene",
		"exit reading mode",
		"stop focus mode",
		"focus mode off",
	} {
		r := Match(text)
		if r == nil || r.Rule != "scene_off" {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want scene_off", text, got)
		}
	}
}

func TestSceneActivationStillMatches(t *testing.T) {
	cases := map[string]string{
		"focus mode":           "scene_focus",
		"reading mode please":  "scene_reading",
		"switch to movie mode": "scene_movie",
		"goodnight":            "scene_night",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}

// Regression: "unmute speaker" used to match the mute_speaker rule because
// anyOf did a bare substring search ("unmute speaker" contains "mute
// speaker") and no unmute rule existed — the device muted on an unmute ask.
func TestMuteUnmuteSpeaker(t *testing.T) {
	cases := map[string]string{
		"unmute speaker":            "unmute_speaker",
		"unmute the speaker please": "unmute_speaker",
		"mute speaker":              "mute_speaker",
		"please mute the speaker":   "mute_speaker",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}

// Off-phrasings scene_off doesn't recognize must NOT activate a scene —
// falling through to the agent (nil) is the correct behavior.
func TestSceneOffNeverActivates(t *testing.T) {
	for _, text := range []string{
		"kill focus mode",
		"i want out of focus mode",
	} {
		if r := Match(text); r != nil && r.Rule != "scene_off" {
			t.Errorf("Match(%q) rule = %s, must not be a scene activation", text, r.Rule)
		}
	}
}

func TestLocalChitchatAttentionAliasesDoNotDependOnVoiceWakeWordGate(t *testing.T) {
	i18n.SetDeviceName("Moon")
	t.Cleanup(func() { i18n.SetDeviceName("autonomous") })

	cases := map[string]string{
		"moon ơi":            "chitchat_attention",
		"này moon, xin chào": "chitchat_greeting",
	}
	for text, want := range cases {
		r := Match(text)
		if r == nil || r.Rule != want {
			got := "<nil>"
			if r != nil {
				got = r.Rule
			}
			t.Errorf("Match(%q) rule = %s, want %s", text, got, want)
		}
	}
}

// TestChitchatWholeWordOnly locks the fix for a substring match that answered
// ordinary sentences as greetings: "hi" sits inside "this", "his", "machine",
// so "What is this?" was replied to with "Hi there!" locally and never reached
// the agent.
func TestChitchatWholeWordOnly(t *testing.T) {
	SetChitchatEnabled(true)
	t.Cleanup(func() { SetChitchatEnabled(true) })

	for _, text := range []string{
		"Body of his arm.",
		"What is this?",
		"This is broken",
		"His name is Tom",
		"The machine is loud",
	} {
		if r := Match(text); r != nil && strings.HasPrefix(r.Rule, "chitchat_") {
			t.Errorf("Match(%q) = %s, want no chitchat match", text, r.Rule)
		}
	}

	for _, text := range []string{"hi", "hello there", "hey", "bye", "thanks a lot"} {
		r := Match(text)
		if r == nil || !strings.HasPrefix(r.Rule, "chitchat_") {
			t.Errorf("Match(%q) = %v, want a chitchat match", text, r)
		}
	}
}

// TestChitchatDisabled covers the realtime case: the voice agent answers social
// talk itself, so os-server must not also fire a canned reply — while command
// intents keep working.
func TestChitchatDisabled(t *testing.T) {
	SetChitchatEnabled(false)
	t.Cleanup(func() { SetChitchatEnabled(true) })

	if r := Match("hi"); r != nil {
		t.Errorf("Match(\"hi\") = %s, want nil when chitchat is off", r.Rule)
	}
	if r := Match("turn on the light"); r == nil || r.Rule != "led_on" {
		t.Errorf("Match(\"turn on the light\") = %v, want led_on", r)
	}
}

// indexPhrase underpins tracking target selection, which needs to know WHERE a
// keyword sits, not just whether it is present.
func TestIndexPhrase(t *testing.T) {
	cases := []struct {
		text, kw string
		want     int
	}{
		{"track my keyboard", "keyboard", 9},
		{"let me know", "me", 4},
		{"watch the camera", "me", -1}, // inside "camera"
		{"track the mouse", "us", -1},  // inside "mouse"
		{"lamp previously mentioned", "me", -1},
		{"me first", "me", 0},  // start boundary
		{"follow me", "me", 7}, // end boundary
		{"unmute speaker", "mute speaker", -1},
		{"mute speaker", "mute speaker", 0},
	}
	for _, c := range cases {
		if got := indexPhrase(c.text, c.kw); got != c.want {
			t.Errorf("indexPhrase(%q, %q) = %d, want %d", c.text, c.kw, got, c.want)
		}
	}
}

// Regression: extractTrackTarget walked its keyword table in declaration order
// with a bare strings.Contains, so the pronoun entry ("me"/"us"/"user", table
// position 3) swallowed every request before "keyboard" (position 14) was ever
// tested — and matched inside unrelated words. Captured on green-lamp
// 2026-09-08: three turns asking the lamp to watch a keyboard all fired
// POST /servo/track {"target":["person"]}.
func TestExtractTrackTarget(t *testing.T) {
	cases := map[string]string{
		// The two real device transcripts that carry a tracking verb.
		"so now i am going to type the word angry on my keyboard. you watch me and tell me if i am tapping in the right way.": "keyboard",
		"let me know when you are ready to track my fingers on my keyboard to type for the word. angry.":                      "keyboard",

		// Object noun beats a pronoun regardless of position.
		"watch me type on my keyboard": "keyboard",
		"track my keyboard":            "keyboard",
		"follow the cup":               "cup",

		// Substring collateral that used to resolve to person.
		"track the mouse":  "mouse",
		"watch the camera": "",

		// Pronouns still work when nothing concrete is named.
		"follow me":        "person",
		"track me":         "person",
		"watch the person": "person",
		"follow that guy":  "person",
	}
	for text, want := range cases {
		if got := extractTrackTarget(text); got != want {
			t.Errorf("extractTrackTarget(%q) = %q, want %q", text, got, want)
		}
	}
}

// extractTrackTarget does NOT gate on the verb — hasTrackVerb does, inside the
// rule's match func. So a bare mention of a noun must not fire the rule even
// though the extractor would happily find a target in it.
func TestTrackRuleNeedsAVerb(t *testing.T) {
	for _, text := range []string{
		"yes, i have a keyboard there. so now you are tracking me and see if i type the word angry right.",
		"i have a keyboard here",
	} {
		if r := MatchCommands(text); r != nil {
			t.Errorf("MatchCommands(%q) = %s, want nil", text, r.Rule)
		}
	}
}

func TestTrackVerbEnd(t *testing.T) {
	cases := map[string]bool{
		"track my keyboard":     true,
		"follow me":             true,
		"you watch me type":     true,
		"you are tracking me":   false, // "tracking" is not "track "
		"i can't watch a movie": true,  // verb present; target extraction decides
		"i have a keyboard":     false,
	}
	for text, want := range cases {
		if got := trackVerbEnd(text) >= 0; got != want {
			t.Errorf("trackVerbEnd(%q) >= 0 = %v, want %v", text, got, want)
		}
	}
}

const envSentence = "so now i am going to type the word angry on my keyboard. you watch me and tell me if i am tapping in the right way."

// The preamble appears and disappears BETWEEN turns of one conversation — it
// depends on the turn's route, not the conversation — so the same sentence must
// resolve identically in every envelope HAL can emit.
func TestEnvelopeInvariance(t *testing.T) {
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {})
	envelopes := map[string]string{
		"delegated with message": "[voice-instruction] user wants the lamp to watch them type\n[transcript] " + envSentence,
		"delegated no message":   envSentence,
		"realtime_not_started":   "unknown speaker: [voice:voice_100] " + envSentence + " (audio saved at /tmp/x.wav)",
		"vision hint prepended":  "[vision-image] /var/lib/hal/snapshots/sensing_look/1.jpg (a photo was just captured for this request)\n[voice-instruction] user wants the lamp to watch them type\n[transcript] " + envSentence,
		"snapshot appended":      "[voice-instruction] user wants the lamp to watch them type\n[transcript] " + envSentence + "\n[snapshot: /var/lib/hal/snapshots/sensing_look/1788839050669.jpg]",
	}
	for name, msg := range envelopes {
		r := MatchCommands(msg)
		if r == nil || r.TTSText != "Tracking keyboard." {
			t.Errorf("%s -> %v, want \"Tracking keyboard.\"", name, r)
		}
	}
}

// A file path must never supply the target: "sensing_face" holds the whole word
// "face" between two non-word characters, and os-server does not strip these
// markers until handler.go:689 — long after the intent match at handler.go:215.
func TestSnapshotPathIsNotATarget(t *testing.T) {
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {})
	for _, msg := range []string{
		"[voice-instruction] user asked the lamp to track the cup\n[transcript] track the cup\n[snapshot: /var/lib/hal/snapshots/sensing_face/1.jpg]",
		"[vision-image] /var/lib/hal/snapshots/sensing_face/1.jpg (a photo was just captured)\n[voice-instruction] track the cup\n[transcript] track the cup",
	} {
		if r := MatchCommands(msg); r == nil || r.TTSText != "Tracking cup." {
			t.Errorf("got %v, want \"Tracking cup.\" — a path set the target", r)
		}
	}
}

// The summary is primary: STT is locked to one language while the user may
// speak another, and the command rules are English-only. Matching the
// transcript alone would silently disable local intent for those users.
func TestSummaryWinsOverGarbledTranscript(t *testing.T) {
	r := MatchCommands("[voice-instruction] turn off the light and play some relaxing music\n[transcript] ton of delay and play some relate music")
	if r == nil || r.Rule != "led_off" {
		t.Fatalf("got %v, want led_off from the summary", r)
	}
}

// No cross-field match: a verb in one field must not combine with a target in
// the other. Neither field alone is a tracking command here.
func TestNoCrossFieldMatch(t *testing.T) {
	if r := MatchCommands("[voice-instruction] the user asked about tracking in general\n[transcript] i have a keyboard here"); r != nil {
		t.Errorf("cross-field match fired %s / %q", r.Rule, r.TTSText)
	}
}

// The three defects that SURVIVE Task 2 and are exactly what matching the
// envelope fields separately fixes: a verb and a target taken from DIFFERENT
// fields, a target taken from a file path, and "me" in a summary — where it
// means the lamp, not the speaker (green-lamp 10:47:58 emitted "User wants to
// connect me with their clock"). None of these is a command, so none may fire.
func TestFieldSeparationDefects(t *testing.T) {
	cases := map[string]string{
		"verb in summary, noun in transcript": "[voice-instruction] the user asked me to track something\n[transcript] there is a keyboard on the desk",
		"only noun is inside a snapshot path": "[voice-instruction] user asked the lamp to track it\n[transcript] track it\n[snapshot: /var/lib/hal/snapshots/sensing_face/1.jpg]",
		"me in a summary means the lamp":      "[voice-instruction] user wants the lamp to follow me into the kitchen\n[transcript] okay",
	}
	for name, msg := range cases {
		if r := MatchCommands(msg); r != nil {
			t.Errorf("%s -> %s / %q, want nil", name, r.Rule, r.TTSText)
		}
	}
}

// End-to-end guard for the three turns captured on green-lamp 2026-09-08, taken
// verbatim from /root/local/flow_events_2026-09-08.jsonl. All three answered
// "Tracking person." and fired POST /servo/track {"target":["person"]}.
// See https://github.com/autonomous-ai/autonomous-os/issues/308
func TestCapturedKeyboardTurns(t *testing.T) {
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {})
	// 10:42:37 — local-intent-1788838957072
	// "keyboard" sits 20 chars EARLIER than "me" and still lost, on table
	// position alone.
	r := MatchCommands("unknown speaker: [voice:voice_100] so now i am going to type the word angry on my keyboard. you watch me and tell me if i am tapping in the right way. (audio saved at /tmp/hal-unknown-voice/voice_100/incoming_1788838954624_577940c2.wav)")
	if r == nil || r.Rule != "servo_track" {
		t.Fatalf("10:42:37 turn = %v, want servo_track", r)
	}
	if want := `POST /servo/track {"target":["keyboard"]}`; len(r.Actions) != 1 || r.Actions[0] != want {
		t.Errorf("10:42:37 actions = %v, want [%s]", r.Actions, want)
	}

	// 10:46:53 — local-intent-1788839213186. "let me know" is filler, not a
	// request to be tracked.
	r = MatchCommands("[voice-instruction] user is asking if the lamp is ready to track their fingers typing the word 'angry'. this follows previous turns about the lamp looking down at the keyboard.\n[transcript] let me know when you are ready to track my fingers on my keyboard to type for the word. angry.")
	if r == nil || r.TTSText != "Tracking keyboard." {
		t.Errorf("10:46:53 turn = %v, want TTS \"Tracking keyboard.\"", r)
	}

	// 10:46:19 — local-intent-1788839179672. The transcript alone is
	// conversational ("you are tracking me"), with no imperative verb, so it
	// matches nothing. The SUMMARY names both the verb and the keyboard, and
	// the summary is the primary field — so the turn resolves to the keyboard
	// rather than to the person, which is the whole point of the fix.
	//
	// Note what this does NOT do: the agent never sees the turn, so nothing
	// checks whether the user typed "angry" correctly. Aiming at the keyboard
	// is the right hardware action; verifying the spelling is a separate
	// request that the local rule still swallows. Making conversational turns
	// fall through to the agent means requiring an imperative in
	// trackVerbEnd — deliberately out of scope here.
	r = MatchCommands("[voice-instruction] user wants lamp to track their typing and confirm if they type 'angry' correctly. lamp previously mentioned not seeing the keyboard, but user is re-requesting based on lamp's 'peeking down' comment.\n[transcript] yes, i have a keyboard there. so now you are tracking me and see if i type the word angry right.")
	if r == nil || r.TTSText != "Tracking keyboard." {
		t.Errorf("10:46:19 turn = %v, want TTS \"Tracking keyboard.\"", r)
	}
}
