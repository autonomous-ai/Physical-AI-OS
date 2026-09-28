package http

import "testing"

// A turn the realtime model delegated already carries that model's filler.
// os-server must not promise an answer again before the main agent does any
// work: no timer at turn start, first filler armed by the first tool.start,
// so a NO_REPLY turn stays silent — device-observed as "one moment" followed
// by nothing.
func TestDelegatedRunArmsOnlyAtFirstTool(t *testing.T) {
	fm := NewFillerManager()
	id := "delegated-turn"
	fm.MarkDelegatedVoiceRun(id, "")
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	run := fm.runs[id]
	if run == nil || run.timer != nil || !run.armOnTool {
		t.Fatal("delegated turn must register without arming a filler")
	}
	fm.OnToolStart(id, "", "web_search")
	if run.timer == nil || run.armOnTool {
		t.Fatal("first non-hardware tool must arm the filler")
	}
}

func TestDelegatedRunHardwareToolDoesNotArm(t *testing.T) {
	fm := NewFillerManager()
	id := "delegated-hw-turn"
	fm.MarkDelegatedVoiceRun(id, "")
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	run := fm.runs[id]
	fm.OnToolStart(id, "http://127.0.0.1:5001/emotion", "curl")
	if run.timer != nil || run.armOnTool {
		t.Fatal("a hardware reaction is the acknowledgement; no filler and no deferred arm")
	}
}

func TestPlainVoiceRunStillArmsAtTurnStart(t *testing.T) {
	fm := NewFillerManager()
	id := "plain-turn"
	fm.MarkVoiceRun(id, "")
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	if fm.runs[id].timer == nil {
		t.Fatal("non-delegated voice turns keep the FillerDelay arm")
	}
}
