package hermes

import (
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"testing"
	"time"
)

func TestEnvironmentQueueDropsExpiredOrSuppressed(t *testing.T) {
	oldSpeaker := speakergate.SpeakerBusy
	speakergate.SpeakerBusy = func() bool { return false }
	t.Cleanup(func() { speakergate.SpeakerBusy = oldSpeaker; sensingmsg.SetEnvironmentReplayAllowed(nil) })
	for _, tc := range []struct {
		name    string
		allowed bool
		age     time.Duration
	}{
		{"expired", true, 61 * time.Second},
		{"sleeping or removed", false, time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			sensingmsg.SetEnvironmentReplayAllowed(func() bool { return tc.allowed })
			s := &HermesService{}
			s.ready.Store(true)
			s.pendingEvents = []pendingEvent{{eventType: "environment.update", msg: "PM changed", queuedAt: time.Now().Add(-tc.age)}}
			s.DrainPendingEvents()
			if len(s.pendingEvents) != 0 || s.inFlightStreams.Load() != 0 {
				t.Fatal("environment event was not discarded")
			}
		})
	}
	if !standaloneDrain(pendingEvent{eventType: "environment.update"}) {
		t.Fatal("environment must retain its dedicated skill routing")
	}
}
