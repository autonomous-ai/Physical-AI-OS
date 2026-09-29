package intent

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/device"
)

var trackingRules = []rule{
	{
		name:       "servo_track_stop",
		capability: device.CapMotion,
		match:      anyOf("stop tracking", "stop following", "stop watching", "stop track"),
		exec: func(string) *Result {
			executionFailed := post("/servo/track/stop", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Stopped tracking.", Actions: []string{"POST /servo/track/stop"}}
		},
	},
	{
		name:       "servo_track",
		capability: device.CapMotion,
		match: func(t string) bool {
			if !hasTrackVerb(t) {
				return false
			}
			return extractTrackTarget(t) != ""
		},
		exec: func(t string) *Result {
			target := extractTrackTarget(t)
			body := fmt.Sprintf(`{"target":["%s"]}`, target)
			executionFailed := post("/servo/track", body) != nil
			return &Result{
				ExecutionFailed: executionFailed,
				TTSText:         fmt.Sprintf("Tracking %s.", target),
				Actions:         []string{"POST /servo/track " + body},
			}
		},
	},
}

// hasTrackVerb returns true when the command contains a tracking verb.
func hasTrackVerb(t string) bool { return trackVerbEnd(t) >= 0 }

// trackVerbEnd returns the index just past the first tracking verb (at a word start), or -1.
func trackVerbEnd(t string) int {
	bestStart, bestEnd := -1, -1
	for _, kw := range []string{"track ", "follow ", "watch "} {
		start := -1
		if strings.HasPrefix(t, kw) {
			start = 0
		} else if j := strings.Index(t, " "+kw); j >= 0 {
			start = j + 1
		}
		if start >= 0 && (bestStart < 0 || start < bestStart) {
			bestStart, bestEnd = start, start+len(kw)
		}
	}
	return bestEnd
}

// trackTargets maps spoken nouns to /servo/track labels; pronoun entries resolve to
// "person" only when no concrete object is named.
var trackTargets = []struct {
	keywords []string
	label    string
	pronoun  bool
}{
	{keywords: []string{"face", "my face", "the face"}, label: "face"},
	{keywords: []string{"hand", "my hand", "the hand"}, label: "hand"},
	{keywords: []string{"me", "myself", "user", "us"}, label: "person", pronoun: true},
	{keywords: []string{"person", "people", "human", "man", "woman", "the guy", "that guy"}, label: "person"},
	{keywords: []string{"dog"}, label: "dog"},
	{keywords: []string{"cat"}, label: "cat"},
	{keywords: []string{"bird"}, label: "bird"},
	{keywords: []string{"cup", "mug", "coffee cup"}, label: "cup"},
	{keywords: []string{"bottle", "water bottle"}, label: "bottle"},
	{keywords: []string{"phone", "smartphone", "cell phone", "mobile"}, label: "cell phone"},
	{keywords: []string{"book"}, label: "book"},
	{keywords: []string{"remote", "tv remote"}, label: "remote"},
	{keywords: []string{"laptop", "computer"}, label: "laptop"},
	{keywords: []string{"keyboard"}, label: "keyboard"},
	{keywords: []string{"mouse"}, label: "mouse"},
	{keywords: []string{"teddy", "teddy bear", "stuffed animal"}, label: "teddy bear"},
	{keywords: []string{"ball"}, label: "sports ball"},
	{keywords: []string{"backpack", "bag"}, label: "backpack"},
	{keywords: []string{"chair"}, label: "chair"},
	{keywords: []string{"clock"}, label: "clock"},
	{keywords: []string{"scissors"}, label: "scissors"},
	{keywords: []string{"banana"}, label: "banana"},
	{keywords: []string{"apple"}, label: "apple"},
	{keywords: []string{"orange"}, label: "orange"},
}

// trackHit is one keyword match: where it starts and what it resolves to.
type trackHit struct {
	idx     int
	label   string
	pronoun bool
}

// extractTrackTarget returns the COCO/face label for a tracking command, or "".
// Whole words only; objects beat pronouns; prefer the first hit after the verb.
func extractTrackTarget(t string) string {
	verbEnd := trackVerbEnd(t)
	var hits []trackHit
	for _, e := range trackTargets {
		best := -1
		for _, kw := range e.keywords {
			if i := indexPhrase(t, kw); i >= 0 && (best < 0 || i < best) {
				best = i
			}
		}
		if best >= 0 {
			hits = append(hits, trackHit{idx: best, label: e.label, pronoun: e.pronoun})
		}
	}
	if label := pickTrackHit(hits, verbEnd, false); label != "" {
		return label
	}
	return pickTrackHit(hits, verbEnd, true)
}

// pickTrackHit returns the first hit after the verb, else the last one before it.
func pickTrackHit(hits []trackHit, verbEnd int, pronouns bool) string {
	after, last := -1, -1
	var afterLabel, lastLabel string
	for _, h := range hits {
		if h.pronoun != pronouns {
			continue
		}
		if verbEnd >= 0 && h.idx >= verbEnd && (after < 0 || h.idx < after) {
			after, afterLabel = h.idx, h.label
		}
		if last < 0 || h.idx > last {
			last, lastLabel = h.idx, h.label
		}
	}
	if afterLabel != "" {
		return afterLabel
	}
	return lastLabel
}
