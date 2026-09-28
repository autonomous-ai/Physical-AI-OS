package http

import (
	"strings"
	"testing"
)

// Pre-tool narration leaves the reply buffer; text already in TTS or carrying a HW marker stays.
func TestDemoteAssistantBufferToThinking(t *testing.T) {
	newHandler := func() *AgentHandler {
		return &AgentHandler{
			assistantBuf:     make(map[string]*strings.Builder),
			streamedCleanLen: make(map[string]int),
		}
	}

	h := newHandler()
	h.accumulateAssistantDelta("run", "Leo's asking if I see him. ")
	h.accumulateAssistantDelta("run", "Let me take a look.")
	if got := h.demoteAssistantBufferToThinking("run"); got != "Leo's asking if I see him. Let me take a look." {
		t.Fatalf("narration not returned: %q", got)
	}
	if text, _ := h.flushAssistantText("run"); text != "" {
		t.Fatalf("narration still in reply buffer: %q", text)
	}
	h.accumulateAssistantDelta("run", "I can't see you right now.")
	if text, _ := h.flushAssistantText("run"); text != "I can't see you right now." {
		t.Fatalf("real reply lost: %q", text)
	}

	h = newHandler()
	h.accumulateAssistantDelta("run", "Sure, off it goes. [HW:/led/off:{}]")
	if got := h.demoteAssistantBufferToThinking("run"); got != "" {
		t.Fatalf("hardware marker demoted: %q", got)
	}
	if _, calls := h.flushAssistantText("run"); len(calls) != 1 {
		t.Fatalf("hardware call lost: %d", len(calls))
	}

	h = newHandler()
	h.accumulateAssistantDelta("run", "Okay, checking. [HW:/led/")
	if got := h.demoteAssistantBufferToThinking("run"); got != "" {
		t.Fatalf("partial hardware marker demoted: %q", got)
	}

	h = newHandler()
	h.accumulateAssistantDelta("run", "Let me look.")
	h.streamedCleanLen["run"] = len("Let me look.")
	if got := h.demoteAssistantBufferToThinking("run"); got != "" {
		t.Fatalf("already-spoken sentence demoted: %q", got)
	}
}
