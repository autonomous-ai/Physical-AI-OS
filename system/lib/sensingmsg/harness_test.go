package sensingmsg

import (
	"strings"
	"testing"
)

func TestReplayHarnessResponseAddress(t *testing.T) {
	SetHarnessConnected(func() bool { return true })
	t.Cleanup(func() { SetHarnessConnected(nil) })
	for _, typ := range []string{"web_chat", "mqtt_chat", "voice_followup"} {
		got := AppendHarnessReplyRoute("[user] ask browser to work", typ, "device-chat-42-1234")
		channel := "web"
		if typ == "voice_followup" {
			channel = "voice"
		}
		if !strings.Contains(got, "[harness-reply run_id=device-chat-42-1234 channel="+channel+"]") {
			t.Fatalf("%s lost response address: %s", typ, got)
		}
	}
	if got := AppendHarnessReplyRoute("sound", "sound", "run"); got != "sound" {
		t.Fatal(got)
	}
}

func TestReplayHarnessRouteChecksCurrentConnection(t *testing.T) {
	SetHarnessConnected(nil)
	t.Cleanup(func() { SetHarnessConnected(nil) })
	const msg = "[user] Find my fan"
	for _, typ := range []string{"web_chat", "mqtt_chat", "voice_followup"} {
		SetHarnessConnected(nil)
		if got := AppendHarnessReplyRoute(msg, typ, "run"); got != msg {
			t.Fatalf("%s added route without connection provider: %q", typ, got)
		}
		connected := false
		SetHarnessConnected(func() bool { return connected })
		for _, state := range []bool{false, true, false, true} {
			connected = state
			got := AppendHarnessReplyRoute(msg, typ, "run")
			if strings.Contains(got, "[harness-reply ") != connected {
				t.Fatalf("%s connected=%t: %q", typ, connected, got)
			}
			if replay := AppendHarnessReplyRoute(got, typ, "run"); replay != got {
				t.Fatalf("%s duplicated route: %q", typ, replay)
			}
		}
	}
}

func TestReplayReplacesStaleHarnessAvailability(t *testing.T) {
	t.Cleanup(func() { SetHarnessConnected(nil) })
	for _, typ := range []string{"web_chat", "mqtt_chat", "voice_followup"} {
		connected := true
		SetHarnessConnected(func() bool { return connected })
		msg := AppendHarnessReplyRoute("[user] Create a report", typ, "run")
		connected = false
		msg = AppendHarnessReplyRoute(msg, typ, "run")
		if strings.Contains(msg, "[harness-reply ") || strings.Contains(msg, HarnessConnectedContext) || strings.Count(msg, HarnessDisconnectedContext) != 1 {
			t.Fatalf("%s stale connected route: %s", typ, msg)
		}
		connected = true
		msg = AppendHarnessReplyRoute(msg, typ, "run")
		if strings.Contains(msg, HarnessDisconnectedContext) || strings.Count(msg, HarnessConnectedContext) != 1 || strings.Count(msg, "[harness-reply ") != 1 {
			t.Fatalf("%s stale offline guidance after reconnect: %s", typ, msg)
		}
	}
}
