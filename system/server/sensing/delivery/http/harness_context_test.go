package http

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/sensingmsg"
)

func TestHarnessContextWithoutConnectionOmitsRemoteRouting(t *testing.T) {
	for _, connected := range []func() bool{nil, func() bool { return false }} {
		h := &SensingHandler{
			harnessConnected:       connected,
			harnessFollowup:        func() bool { t.Fatal("read follow-up while disconnected"); return true },
			harnessFollowupContext: func() string { t.Fatal("read stale context"); return "old answer" },
		}
		for _, channel := range []string{"voice", "web"} {
			for _, message := range []string{"Find my fan", "Ask Harness to do this", "Is it done?"} {
				want := ""
				if connected != nil {
					want = "\n" + sensingmsg.HarnessDisconnectedContext
				}
				if got := h.harnessRoutingContext(message, "run-1", channel); got != want {
					t.Fatalf("unexpected disconnected context: %q", got)
				}
			}
		}
	}
}

func TestHarnessContextTracksConnectionAndPreservesConnectedRouting(t *testing.T) {
	connected := true
	h := &SensingHandler{
		harnessConnected:       func() bool { return connected },
		harnessFollowup:        func() bool { return true },
		harnessFollowupContext: func() string { return "Saved answer" },
	}
	for _, channel := range []string{"voice", "web"} {
		got := h.harnessRoutingContext("Ask Harness to do this", "run-1", channel)
		if !strings.Contains(got, "[harness-reply run_id=run-1 channel="+channel+"]") ||
			!strings.Contains(got, harnessNamedAgentRouting) || !strings.Contains(got, "Saved answer") {
			t.Fatalf("missing connected context: %q", got)
		}
	}
	connected = false
	if got := h.harnessRoutingContext("Is it done?", "run-2", "voice"); got != "\n"+sensingmsg.HarnessDisconnectedContext {
		t.Fatalf("disconnect retained context: %q", got)
	}
	connected = true
	if got := h.harnessRoutingContext("Is it done?", "run-3", "voice"); !strings.Contains(got, "run_id=run-3") {
		t.Fatalf("reconnect lost context: %q", got)
	}
}
