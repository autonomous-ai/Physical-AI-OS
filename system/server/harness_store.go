package server

import (
	"fmt"
	"strings"
	"unicode"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/flow"
)

// observeHarnessPreparation projects observed RPC snapshots only. Preparation
// never claims the response route: the main agent still owns guidance and speech.
func (s *Server) observeHarnessPreparation(kind string, reply *harnessReplyRequest, result harness.Frame) {
	if reply == nil || (kind != "agent.prepare" && kind != "operation.get") {
		return
	}
	operation, ok := result["operation"].(map[string]any)
	if !ok {
		return
	}
	text := harnessPreparationText(operation)
	if text == "" {
		return
	}
	flow.Log("harness_store_progress", map[string]any{
		"run_id": reply.RunID, "operation_id": boundedHarnessStoreText(operation["operationId"], 128),
		"state": boundedHarnessStoreText(operation["state"], 40), "phase": boundedHarnessStoreText(operation["phase"], 40), "text": text,
	}, reply.RunID)
	if s.agentHandler != nil {
		s.agentHandler.DeliverHarnessPreparationProgress(reply.RunID, boundedHarnessStoreText(operation["operationId"], 128), text)
	}
}

func harnessPreparationText(operation map[string]any) string {
	state, _ := operation["state"].(string)
	switch state {
	case "accepted":
		return "Harness accepted agent preparation; the task has not been sent."
	case "running":
		phase := boundedHarnessStoreText(operation["phase"], 40)
		if phase == "" {
			phase = "in progress"
		}
		return fmt.Sprintf("Harness agent preparation: %s. The task has not been sent.", phase)
	case "ready":
		return "Harness agent preparation is ready; the task has not been sent. Engine authentication and task success remain unverified."
	case "failed", "needs_user_action":
		text := "Harness agent preparation failed."
		if state == "needs_user_action" {
			text = "Harness agent preparation needs user action."
		}
		// Remote diagnostics are untrusted display content, never instructions to
		// execute locally. Keep their size bounded and leave raw doctor output out.
		if failure, ok := operation["error"].(map[string]any); ok {
			if code := boundedHarnessStoreText(failure["code"], 100); code != "" {
				text += " [" + code + "]"
			}
			if message := boundedHarnessStoreText(failure["message"], 1200); message != "" {
				text += " Harness reports: " + message
			}
		}
		if guidance := boundedHarnessStoreText(operation["guidance"], 2000); guidance != "" {
			text += " Harness guidance: " + guidance
		}
		return text + " The task has not been sent."
	default:
		return ""
	}
}

func boundedHarnessStoreText(value any, limit int) string {
	text, _ := value.(string)
	text = strings.TrimSpace(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, text))
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit]) + "…"
	}
	return text
}
