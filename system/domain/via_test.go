package domain

import "testing"

func TestAppendVia(t *testing.T) {
	tests := []struct{ msg, source, want string }{
		{"[user] hello", "mobile", "[user] hello\n[via:mobile]"},
		{"Summarize my calendar.", ViaSchedule, "Summarize my calendar.\n[via:schedule]"},
		{"/status", "web", "/status"},                              // command router needs the literal text
		{"[user] hi\n[via:web]", "mobile", "[user] hi\n[via:web]"}, // never stamped twice
		{"", "web", ""},
		{"[user] hi", "", "[user] hi"},
	}
	for _, tt := range tests {
		if got := AppendVia(tt.msg, tt.source); got != tt.want {
			t.Errorf("AppendVia(%q, %q) = %q, want %q", tt.msg, tt.source, got, tt.want)
		}
	}
}
