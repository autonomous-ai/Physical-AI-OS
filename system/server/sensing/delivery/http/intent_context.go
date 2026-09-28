package http

import (
	"go.autonomous.ai/os/system/intent/jev"
	"log/slog"
	"regexp"
	"strings"
)

var physicalIntentTarget = regexp.MustCompile(`(?i)(?:\b(?:lamp|light|lights|speaker|volume|servo|your camera)\b|đèn|âm lượng|loa|den lamp)`)
var contextualAdjustment = regexp.MustCompile(`(?i)\b(?:brighter|dimmer|darker|louder|quieter|warmer|cooler)\b`)

var digitalIntentTarget = regexp.MustCompile(`(?i)\b(?:render|image|photo|picture|video|canvas|document|website|harness|agent|blender)\b`)

// SetHarnessTaskPending supplies existing response ownership, not Store state.
func (h *SensingHandler) SetHarnessTaskPending(fn func() bool) { h.harnessTaskPending = fn }

// This gate only defers to the main runtime. It never selects an agent, starts
// preparation, dispatches Harness work, or treats connection as task context.
func (h *SensingHandler) deferContextualIntent(message string) (deferToMain bool) {
	defer func() {
		if deferToMain {
			slog.Info("intent routing deferred", "component", "sensing", "reason", "context_requires_main")
		}
	}()
	text := strings.ToLower(strings.TrimSpace(jev.NormalizeText(message)))
	if text == "" {
		return true
	}
	fragment := strings.Trim(text, ".!? ")
	switch fragment {
	case "brighter", "dimmer", "darker", "louder", "quieter", "warmer", "cooler", "energize", "max brightness", "stop", "continue", "yes", "no", "do it", "try again":
		return true // Store preparation is not observable here; never guess its referent.
	}
	if contextualAdjustment.MatchString(text) && !physicalIntentTarget.MatchString(text) {
		return true
	}
	pending := h.harnessTaskPending != nil && h.harnessTaskPending()
	hint := h.harnessFollowup != nil && h.harnessFollowup()
	if !pending && !hint {
		return false
	}
	return !physicalIntentTarget.MatchString(text) || digitalIntentTarget.MatchString(text)
}
