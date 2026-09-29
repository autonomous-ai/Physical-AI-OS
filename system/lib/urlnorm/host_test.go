package urlnorm

import "testing"

func TestIsAutonomousHost(t *testing.T) {
	cases := []struct {
		url  string
		want bool
	}{
		{"https://device-api.autonomous.ai/api/v1/ai/v1", true},
		{"https://DEVICE-API.Autonomous.AI/api/v1/ai/v1", true},
		{"https://device-api.staging.autonomousdev.xyz/api/v1/ai/v1", true},
		{"https://autonomous.ai/v1", true},
		// Uncertainty answers false: callers must not send data on a guess.
		{"", false},
		{"::not a url::", false},
		{"not-a-url-either", false},
		{"https://openrouter.ai/api/v1", false},
		{"http://localhost:11434/v1", false},
		{"https://autonomous.ai.evil.example.com/v1", false}, // suffix must be a real label boundary
		{"https://evilautonomous.ai/v1", false},
	}
	for _, c := range cases {
		if got := IsAutonomousHost(c.url); got != c.want {
			t.Errorf("IsAutonomousHost(%q) = %v, want %v", c.url, got, c.want)
		}
	}
}
