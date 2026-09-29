package hermes

import (
	"strings"
	"testing"
	"time"
)

func TestStandaloneDrain(t *testing.T) {
	cases := []struct {
		name string
		ev   pendingEvent
		want bool
	}{
		{"voice command keeps own turn", pendingEvent{eventType: "voice_command"}, true},
		{"ambient voice keeps own turn", pendingEvent{eventType: "voice"}, true},
		{"realtime-handled is silent, standalone", pendingEvent{eventType: "voice_agent_handled"}, true},
		{"image-bearing is standalone", pendingEvent{eventType: "presence.enter", images: []string{"base64=="}}, true},
		{"presence is mergeable", pendingEvent{eventType: "presence.enter"}, false},
		{"motion is mergeable", pendingEvent{eventType: "motion.activity"}, false},
		{"emotion is mergeable", pendingEvent{eventType: "emotion.detected"}, false},
		{"speech_emotion is mergeable", pendingEvent{eventType: "speech_emotion.detected"}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := standaloneDrain(tc.ev); got != tc.want {
				t.Errorf("standaloneDrain(%+v) = %v, want %v", tc.ev, got, tc.want)
			}
		})
	}
}

func TestBuildMergedSensing(t *testing.T) {
	now := time.Now()
	evs := []pendingEvent{
		{eventType: "presence.enter", msg: "someone entered", queuedAt: now.Add(-3 * time.Second)},
		{eventType: "emotion.detected", msg: "happy", queuedAt: now.Add(-2 * time.Second)},
		{eventType: "motion.activity", msg: "typing at desk", queuedAt: now.Add(-1 * time.Second)},
	}

	merged, types, oldest := buildMergedSensing(evs)

	if !strings.HasPrefix(merged, "[ambient signals batched") {
		t.Errorf("merged should start with the batch header, got:\n%s", merged)
	}
	for _, want := range []string{"[sensing:presence.enter]", "[emotion]", "[activity]"} {
		if !strings.Contains(merged, want) {
			t.Errorf("merged missing %q:\n%s", want, merged)
		}
	}
	wantTypes := []string{"presence.enter", "emotion.detected", "motion.activity"}
	if len(types) != len(wantTypes) {
		t.Fatalf("types = %v, want %v", types, wantTypes)
	}
	for i, want := range wantTypes {
		if types[i] != want {
			t.Errorf("types[%d] = %q, want %q", i, types[i], want)
		}
	}
	if !oldest.Equal(now.Add(-3 * time.Second)) {
		t.Errorf("oldest = %v, want %v", oldest, now.Add(-3*time.Second))
	}
}

func TestBuildMergedSensingEmpty(t *testing.T) {
	merged, types, _ := buildMergedSensing(nil)
	if merged != "" {
		t.Errorf("merged = %q, want empty for no events", merged)
	}
	if len(types) != 0 {
		t.Errorf("types = %v, want empty", types)
	}
}
