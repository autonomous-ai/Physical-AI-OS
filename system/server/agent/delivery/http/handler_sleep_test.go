package http

import (
	"errors"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestSleepGateUsesHALAfterServerRestart(t *testing.T) {
	t.Setenv("DEVICE_TYPE", "lamp")
	t.Setenv("DEVICES_DIR", t.TempDir())
	for _, tc := range []struct {
		name        string
		lastEmotion string
		halSleeping bool
		halErr      error
		want        bool
	}{
		{name: "restart while HAL asleep", halSleeping: true, want: true},
		{name: "restart while HAL awake"},
		{name: "direct HAL sleep", lastEmotion: "happy", halSleeping: true, want: true},
		{name: "button wake", lastEmotion: "sleepy"},
		{name: "HAL unavailable while believed asleep", lastEmotion: "sleepy", halErr: errors.New("unavailable"), want: true},
		{name: "HAL unavailable while believed awake", lastEmotion: "happy", halErr: errors.New("unavailable")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := &AgentHandler{config: &config.Config{DeviceType: "lamp"}, lastEmotion: tc.lastEmotion}
			calls := 0
			got := h.isSleeping(func() (bool, error) {
				calls++
				return tc.halSleeping, tc.halErr
			})
			if got != tc.want {
				t.Errorf("IsSleeping() = %v, want %v", got, tc.want)
			}
			if calls != 1 {
				t.Errorf("HAL calls = %d, want 1", calls)
			}
		})
	}
}
