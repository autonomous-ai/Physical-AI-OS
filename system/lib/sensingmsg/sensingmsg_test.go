package sensingmsg

import (
	"strings"
	"testing"
)

func TestBuildVoiceFollowupIsAnAuthorizedUserTurn(t *testing.T) {
	got := Build("voice_followup", "play music", "", "")
	if !strings.HasPrefix(got, "[user] play music") {
		t.Fatalf("voice_followup = %q, want user-priority message", got)
	}
	if strings.Contains(got, "[ambient]") {
		t.Fatalf("voice_followup must not be marked ambient: %q", got)
	}
}

func TestBuildPresenceEnterCarriesCurrentUser(t *testing.T) {
	got := Build("presence.enter", "Person detected — new: friend (long); faces in frame: 1 (long)", "long", "")
	if !strings.Contains(got, "[context: current_user=long]") {
		t.Fatalf("presence.enter = %q, want current_user attribution", got)
	}
}

func TestBuildPresenceEnterUnknownUserIsLabelledUnknown(t *testing.T) {
	// Strangers must still carry the tag; greeting routes key "no name" off current_user=unknown.
	got := Build("presence.enter", "Person detected — new: stranger (stranger_3); faces in frame: 1 (stranger_3)", "", "")
	if !strings.Contains(got, "[context: current_user=unknown]") {
		t.Fatalf("presence.enter with no user = %q, want current_user=unknown", got)
	}
}

func TestEnvironmentUsesDedicatedSkill(t *testing.T) {
	got := Build("environment.update", "PM2.5 changed", "", "")
	if !strings.HasPrefix(got, "[environment:update] PM2.5 changed") || !strings.Contains(got, "No mandatory speech or emotion") {
		t.Fatalf("environment routing = %q", got)
	}
	if strings.Contains(got, "[sensing:") || strings.Contains(got, "[guard-active]") {
		t.Fatalf("unexpected sensing/guard routing: %q", got)
	}
}

func TestBuildPresenceEnterNewFriendCarriesPresenceContext(t *testing.T) {
	got := Build("presence.enter", "Person detected — new: friend (long); faces in frame: 1 (long)", "long", "")
	if !strings.Contains(got, "[presence_context:") {
		t.Fatalf("friend presence.enter = %q, want presence_context block", got)
	}
}

func TestBuildPresenceEnterStrangerJoiningPresentFriendSkipsPresenceContext(t *testing.T) {
	// A stranger arriving while a friend is still current_user must not get the friend's block.
	got := Build("presence.enter",
		"Person detected — new: stranger (stranger_1); already present: long (friend); faces in frame: 2 (long, stranger_1)",
		"long", "")
	if strings.Contains(got, "[presence_context:") {
		t.Fatalf("stranger-only presence.enter = %q, must not carry presence_context", got)
	}
	if !strings.Contains(got, "[context: current_user=long]") {
		t.Fatalf("stranger-only presence.enter = %q, attribution tag must stay", got)
	}
}

func TestBuildPresenceEnterLoneStrangerInsideFriendWindowSkipsPresenceContext(t *testing.T) {
	// Same current_user, friend out of frame: the block would be equally wrong.
	got := Build("presence.enter", "Person detected — new: stranger (stranger_4); faces in frame: 1 (stranger_4)", "long", "")
	if strings.Contains(got, "[presence_context:") {
		t.Fatalf("lone-stranger presence.enter = %q, must not carry presence_context", got)
	}
}

func TestBuildPresenceEnterStrangerJoiningPresentFriendCarriesInlineRule(t *testing.T) {
	// The rule must ride inline; runtimes may skip loading sensing/SKILL.md.
	got := Build("presence.enter",
		"Person detected — new: stranger (stranger_1); already present: long (friend); faces in frame: 2 (long, stranger_1)",
		"long", "")
	if !strings.Contains(got, "[A stranger joined long, who is in frame — speak to long, not to the stranger.") {
		t.Fatalf("stranger-joins-friend presence.enter = %q, want inline rule naming the friend", got)
	}
}

func TestBuildPresenceEnterLoneStrangerHasNoJoinRule(t *testing.T) {
	got := Build("presence.enter", "Person detected — new: stranger (stranger_4); faces in frame: 1 (stranger_4)", "long", "")
	if strings.Contains(got, "[A stranger joined") {
		t.Fatalf("lone-stranger presence.enter = %q, must not carry the join rule", got)
	}
}

func TestBuildPresenceEnterNewFriendHasNoJoinRule(t *testing.T) {
	got := Build("presence.enter",
		"Person detected — new: friend (leo); already present: long (friend); faces in frame: 2 (long, leo)",
		"leo", "")
	if strings.Contains(got, "[A stranger joined") {
		t.Fatalf("friend-joins-friend presence.enter = %q, must not carry the stranger join rule", got)
	}
}

func TestTurnSource(t *testing.T) {
	tests := []struct {
		eventType, message, want string
	}{
		{"web_chat", "hello", "web"},
		{"mqtt_chat", "hello", "mobile"},
		{"voice_command", "turn off the lights", "voice"},
		{"voice_followup", "and the fan", "voice"},
		{"voice_command", "[voice-instruction] Turn off the LEDs\n[transcript] …", "voice_handoff"},
		{"voice", "[voice-instruction] Check my email\n[transcript] …", "voice_handoff"},
		{"voice", "Unknown Speaker: [voice:voice_7] Clearly a disease.", "voice_ambient"},
		{"voice_agent_handled", "[HANDLED] \"…\"", "voice_history"},
		{"presence.enter", "Person detected", "sensing"},
	}
	for _, tt := range tests {
		if got := TurnSource(tt.eventType, tt.message); got != tt.want {
			t.Errorf("TurnSource(%q, %q) = %q, want %q", tt.eventType, tt.message, got, tt.want)
		}
	}
}

func TestBuildStampsExplicitSource(t *testing.T) {
	tests := []struct{ eventType, message, wantPrefix, wantVia string }{
		{"mqtt_chat", "turn on the fan", "[user] turn on the fan", "[via:mobile]"},
		{"web_chat", "turn on the fan", "[user] turn on the fan", "[via:web]"},
		{"voice_command", "turn on the fan", "[user] turn on the fan", "[via:voice]"},
		{"voice", "overheard", "[user] [ambient] overheard", "[via:voice_ambient]"},
		{"motion.activity", "sitting", "[activity] sitting", "[via:sensing]"},
	}
	for _, tt := range tests {
		got := Build(tt.eventType, tt.message, "", "")
		if !strings.HasPrefix(got, tt.wantPrefix) || !strings.HasSuffix(got, "\n"+tt.wantVia) {
			t.Errorf("Build(%q) = %q, want prefix %q and last line %q", tt.eventType, got, tt.wantPrefix, tt.wantVia)
		}
	}
	if got := Build("web_chat", "/status", "", ""); got != "/status" {
		t.Errorf("slash command must pass through verbatim, got %q", got)
	}
}
