package http

import "testing"

// Reading a skill file that mentions /emotion must not count as a hardware event (#342).
func TestReadingASkillFileIsNotAHardwareEvent(t *testing.T) {
	for _, args := range []string{
		`{"command":"/bin/bash -lc 'cat /root/skills/emotion/SKILL.md | head -100'"}`,
		`{"command":"grep -rn /servo/aim /root/skills/"}`,
		`{"command":"cat docs/led-control.md"}`,
		`{"command":"echo 'the /emotion endpoint takes a name'"}`,
	} {
		if got := hwPathFromToolArgs(args); got != "" {
			t.Errorf("a non-call was treated as hardware: %q -> %q", args, got)
		}
	}
}

func TestARealHardwareCallIsResolvedToItsPath(t *testing.T) {
	for _, tc := range []struct{ args, want string }{
		{`{"command":"curl -sX POST http://127.0.0.1:5001/emotion -d '{\"emotion\":\"happy\"}'"}`, "/emotion"},
		{`{"command":"curl -sX POST http://127.0.0.1:5001/servo/aim -H 'Content-Type: application/json' -d '{\"direction\":\"right\"}'"}`, "/servo/aim"},
		{`{"command":"curl -s 'http://127.0.0.1:5001/camera/snapshot?save=true&width=768'"}`, "/camera/snapshot"},
		{`{"command":"curl -sX POST http://127.0.0.1:5000/api/vision/look -d '{}'"}`, "/api/vision/look"},
		{`/bin/bash -lc "curl -sX POST http://127.0.0.1:5001/led/off"`, "/led/off"},
	} {
		if got := hwPathFromToolArgs(tc.args); got != tc.want {
			t.Errorf("%s\n  got %q want %q", tc.args, got, tc.want)
		}
	}
}

// Every body-moving servo endpoint counts as a servo event; reads do not.
func TestEveryBodyMovingServoCallCountsAsAServoEvent(t *testing.T) {
	for _, path := range []string{
		"/servo/aim", "/servo/play", "/servo/search", "/servo/nudge", "/servo/demo",
	} {
		args := `{"command":"curl -sX POST http://127.0.0.1:5001` + path + ` -d '{}'"}`
		if !isServoMovementPath(hwPathFromToolArgs(args)) {
			t.Errorf("%s does not count as a servo event", path)
		}
	}
	for _, path := range []string{"/servo/position", "/servo/status", "/servo/bearing"} {
		args := `{"command":"curl -s http://127.0.0.1:5001` + path + `"}`
		if isServoMovementPath(hwPathFromToolArgs(args)) {
			t.Errorf("%s is a read, not a movement", path)
		}
	}
}

// The first HAL call wins over an earlier mention in the same command.
func TestTheCallWinsOverAMentionInTheSameCommand(t *testing.T) {
	args := `{"command":"cat /root/skills/emotion/SKILL.md; curl -sX POST http://127.0.0.1:5001/emotion -d '{}'"}`
	if got := hwPathFromToolArgs(args); got != "/emotion" {
		t.Errorf("got %q want /emotion", got)
	}
}
