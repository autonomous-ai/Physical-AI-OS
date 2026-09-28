package ambient

import (
	"errors"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestAmbientRestartRespectsHALSleep(t *testing.T) {
	for _, tc := range []struct {
		name     string
		sleeping bool
		err      error
		want     bool
	}{
		{"awake", false, nil, false},
		{"restart asleep", true, nil, true},
		{"HAL unavailable", false, errors.New("offline"), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := &Service{cfg: &config.Config{DeviceType: "lamp"}}
			if got := s.isPausedWithSleep(func() (bool, error) { return tc.sleeping, tc.err }); got != tc.want {
				t.Fatalf("paused=%v, want %v", got, tc.want)
			}
		})
	}
}
