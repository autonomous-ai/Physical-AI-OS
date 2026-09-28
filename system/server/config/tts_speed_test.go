package config

import "testing"

func TestGetTTSSpeedLegacyEnvironment(t *testing.T) {
	for _, tc := range []struct {
		env  string
		want float64
	}{
		{"", 1.2}, {"1.1", 1.1}, {" 1.1 ", 1.1}, {"1.3", 1.3}, {"0.5", 0.5},
		{"0", 0.25}, {"-1", 0.25}, {"invalid", 1.2}, {"NaN", 1.2}, {"Inf", 1.2}, {"-Inf", 1.2}, {"1e999", 1.2}, {"5", 4},
	} {
		t.Run(tc.env, func(t *testing.T) {
			t.Setenv("HAL_TTS_SPEED", tc.env)
			cfg := &Config{}
			if got := cfg.GetTTSSpeed(); got != tc.want {
				t.Fatalf("GetTTSSpeed() = %v, want %v", got, tc.want)
			}
			saved := 1.0
			cfg.TTSSpeed = &saved
			if got := cfg.GetTTSSpeed(); got != 1 {
				t.Fatalf("saved speed must win, got %v", got)
			}
		})
	}
}
