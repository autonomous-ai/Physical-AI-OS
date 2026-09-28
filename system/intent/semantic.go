package intent

import (
	"context"
	"slices"
	"strings"

	"go.autonomous.ai/os/system/intent/jev"
)

// SemanticResolver recognizes an action with bounded parameters; it never executes hardware.
type SemanticResolver interface {
	Resolve(context.Context, string, []jev.Candidate, jev.Options) jev.Selection
}

// MatchWithFallback executes complete canonical commands locally. Contextual
// requests reach the optional resolver before any hardware action. Once execution is attempted its result stays
// handled, including failure, so the main agent cannot repeat a partial action.
func MatchWithFallback(ctx context.Context, text string, resolver SemanticResolver, options jev.Options) *Result {
	if ctx.Err() != nil {
		return nil
	}
	if result := matchCanonical(text); result != nil {
		return result
	}
	if resolver == nil || !options.Enabled || strings.TrimSpace(options.Endpoint) == "" || strings.TrimSpace(options.APIKey) == "" {
		return nil
	}
	candidates := semanticCandidates()
	if len(candidates) == 0 {
		return nil
	}
	selection := resolver.Resolve(ctx, text, candidates, options)
	if ctx.Err() != nil {
		return nil
	}
	selected := semanticCommand(selection, candidates)
	result := selected.execute()
	if result != nil {
		result.Source = "jev"
	}
	return result
}

func semanticCandidates() []jev.Candidate {
	var candidates []jev.Candidate
	for _, candidate := range jev.Candidates() {
		for _, r := range rules {
			// Legacy local rules allow an unknown body; semantic execution
			// instead requires positive evidence for each capability.
			if r.name == candidate.ID && semanticCapEnabled(r.capability) {
				candidates = append(candidates, candidate)
				break
			}
		}
	}
	return candidates
}

// Hardware-free rules remain available even when the body is unknown.
func semanticCapEnabled(capability string) bool {
	return capability == "" || deviceCaps[capability]
}

func semanticCommand(selection jev.Selection, offered []jev.Candidate) *command {
	for _, candidate := range offered {
		if candidate.ID != selection.Intent {
			continue
		}
		if len(selection.Parameters) != len(candidate.Parameters) {
			return nil
		}
		for name, parameter := range candidate.Parameters {
			if !slices.Contains(parameter.Options, selection.Parameters[name]) {
				return nil
			}
		}
		text, ok := semanticExecutionText(selection)
		if !ok {
			return nil
		}
		for i := range rules {
			r := &rules[i]
			if r.name == selection.Intent && semanticCapEnabled(r.capability) {
				return &command{rule: r, text: text}
			}
		}
	}
	return nil
}

// Translate validated enum values into code-owned input for the existing rule.
// Never pass the user's utterance or a provider-generated HAL payload to exec.
func semanticExecutionText(selection jev.Selection) (string, bool) {
	switch selection.Intent {
	case "led_color":
		color := selection.Parameters["color"]
		for _, option := range colorKeywords {
			if color == option.keywords[0] && len(selection.Parameters) == 1 {
				return "set the light " + color, true
			}
		}
		return "", false
	case "servo_track":
		target := selection.Parameters["target"]
		for _, option := range trackTargets {
			if target == option.label && len(selection.Parameters) == 1 {
				return "track " + target, true
			}
		}
		return "", false
	default:
		return "", len(selection.Parameters) == 0
	}
}
