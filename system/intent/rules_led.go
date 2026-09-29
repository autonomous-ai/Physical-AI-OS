package intent

import (
	"fmt"
	"go.autonomous.ai/os/system/lib/hal"
	"log/slog"
	"math"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/device"
)

// colorKeywords maps color keywords to RGB; first match wins.
var colorKeywords = []struct {
	keywords []string
	rgb      [3]int
	name     string
}{
	{[]string{"yellow"}, [3]int{255, 220, 0}, "Yellow"},
	{[]string{"red"}, [3]int{255, 0, 0}, "Red"},
	{[]string{"green"}, [3]int{0, 200, 100}, "Green"},
	{[]string{"blue"}, [3]int{0, 100, 255}, "Blue"},
	{[]string{"cyan"}, [3]int{0, 200, 150}, "Cyan"},
	{[]string{"purple", "violet"}, [3]int{100, 50, 200}, "Purple"},
	{[]string{"orange"}, [3]int{255, 100, 0}, "Orange"},
	{[]string{"pink"}, [3]int{255, 80, 150}, "Pink"},
	{[]string{"white"}, [3]int{255, 255, 255}, "White"},
	{[]string{"warm"}, [3]int{255, 180, 100}, "Warm"},
}

// extractColor returns the RGB and name for the first color keyword found in t.
func extractColor(t string) ([3]int, string, bool) {
	for _, c := range colorKeywords {
		for _, kw := range c.keywords {
			if strings.Contains(t, kw) {
				return c.rgb, c.name, true
			}
		}
	}
	return [3]int{}, "", false
}

// isLEDOnCommand reports whether t contains a "turn on light" phrase.
func isLEDOnCommand(t string) bool {
	triggers := []string{"turn on the light", "light on", "set color", "change color", "set the light"}
	for _, kw := range triggers {
		if strings.Contains(t, kw) {
			return true
		}
	}
	return false
}

var ledRules = []rule{
	// LED color must run before generic LED on/off.
	{
		name:       "led_color",
		capability: device.CapLight,
		match: func(t string) bool {
			if !isLEDOnCommand(t) {
				return false
			}
			_, _, ok := extractColor(t)
			return ok
		},
		exec: func(t string) *Result {
			rgb, name, _ := extractColor(t)
			executionFailed := post("/led/effect/stop", "") != nil
			body := fmt.Sprintf(`{"color":[%d,%d,%d]}`, rgb[0], rgb[1], rgb[2])
			executionFailed = post("/led/solid", body) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: name + " light on!", LEDChanged: true, Actions: []string{"POST /led/effect/stop", "POST /led/solid " + body}}
		},
	},

	{
		name:       "led_on",
		capability: device.CapLight,
		match:      anyOf("turn on the light", "light on"),
		exec: func(string) *Result {
			executionFailed := post("/led/solid", `{"color":[255,220,180]}`) != nil
			executionFailed = postEmotion(`{"emotion":"happy","intensity":0.6}`) != nil || executionFailed
			return &Result{ExecutionFailed: executionFailed, TTSText: "Light on!", LEDChanged: true, Actions: []string{`POST /led/solid {"color":[255,220,180]}`, `POST /emotion {"emotion":"happy","intensity":0.6}`}}
		},
	},
	{
		name:       "led_off",
		capability: device.CapLight,
		match:      anyOf("turn off the light", "light off"),
		exec: func(string) *Result {
			// No emotion after /led/off: any emotion would re-light the strip.
			executionFailed := post("/led/off", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Light off!", LEDOff: true, Actions: []string{"POST /led/off"}}
		},
	},

	{
		name:       "brighten",
		capability: device.CapLight,
		match:      anyOf("brighten the light", "brighten light", "brighter"),
		exec:       func(string) *Result { return brightenCurrentLight() },
	},

	{
		name:       "dim",
		capability: device.CapLight,
		match:      anyOf("dim the light", "dimmer", "dim light"),
		exec:       func(string) *Result { return dimCurrentLight() },
	},
}

// Serialize dim read/write pairs so simultaneous requests each reduce the level.
var dimMu sync.Mutex

func dimCurrentLight() *Result {
	if !dimMu.TryLock() {
		return &Result{ExecutionFailed: true, TTSText: "I couldn't change that setting because another adjustment is in progress. Please try again."}
	}
	defer dimMu.Unlock()
	actions := []string{"GET /led/color"}
	failure := func() *Result {
		return &Result{ExecutionFailed: true, TTSText: "I couldn't dim the light.", Actions: actions}
	}
	color, err := hal.GetColor()
	if err != nil {
		slog.Warn("intent dim read failed", "error", err)
		return failure()
	}
	for _, channel := range color {
		if channel < 0 || channel > 255 {
			return failure()
		}
	}
	if color == [3]int{} {
		return &Result{TTSText: "The light is already off.", Actions: actions}
	}
	// Scale every channel equally; integer rounding can reach black at low levels.
	next := [3]int{color[0] / 2, color[1] / 2, color[2] / 2}
	body := fmt.Sprintf(`{"color":[%d,%d,%d]}`, next[0], next[1], next[2])
	actions = append(actions, "POST /led/solid "+body)
	if err := post("/led/solid", body); err != nil {
		return failure()
	}
	actions = append(actions, "GET /led/color")
	actual, err := hal.GetColor()
	if err != nil || actual != next {
		slog.Warn("intent dim verification failed", "expected", next, "actual", actual, "error", err)
		return failure()
	}
	slog.Info("intent dim applied", "before", color, "after", next)
	return &Result{TTSText: "Dimmed.", LEDChanged: next != [3]int{}, LEDOff: next == [3]int{}, Actions: actions}
}

// brightenCurrentLight shares the dim lock so opposite adjustments cannot overwrite each other.
func brightenCurrentLight() *Result {
	if !dimMu.TryLock() {
		return &Result{ExecutionFailed: true, TTSText: "I couldn't change that setting because another adjustment is in progress. Please try again."}
	}
	defer dimMu.Unlock()
	actions := []string{"GET /led/color"}
	failure := func() *Result {
		return &Result{ExecutionFailed: true, TTSText: "I couldn't confirm the light got brighter.", Actions: actions}
	}
	color, err := hal.GetColor()
	if err != nil {
		return failure()
	}
	peak := max(color[0], color[1], color[2])
	if peak == 255 {
		return &Result{TTSText: "The light is already at maximum brightness.", Actions: actions}
	}
	// Start a dark lamp gently; otherwise raise its peak by 25%, retaining its hue.
	next := [3]int{32, 27, 20}
	if peak > 0 {
		nextPeak := min(255, peak+max(1, peak/4))
		for i, channel := range color {
			next[i] = channel * nextPeak / peak
		}
	}
	body := fmt.Sprintf(`{"color":[%d,%d,%d]}`, next[0], next[1], next[2])
	actions = append(actions, "POST /led/solid "+body)
	if err := post("/led/solid", body); err != nil {
		return failure()
	}
	actions = append(actions, "GET /led/color")
	actual, err := hal.GetColor()
	if err != nil {
		return failure()
	}
	actualPeak, requestedPeak := max(actual[0], actual[1], actual[2]), max(next[0], next[1], next[2])
	if actualPeak <= peak || actualPeak > requestedPeak {
		return failure()
	}
	// HAL can proportionally clamp RGB to its safety ceiling. Accept that only
	// when readback confirms an actual increase and the expected scaled hue.
	for i, channel := range next {
		if actual[i] != int(math.RoundToEven(float64(channel)*float64(actualPeak)/float64(requestedPeak))) {
			return failure()
		}
	}
	return &Result{TTSText: "Brighter now.", LEDChanged: true, Actions: actions}
}
