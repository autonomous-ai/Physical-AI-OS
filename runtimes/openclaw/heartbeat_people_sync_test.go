package openclaw

import (
	"regexp"
	"strings"
	"testing"
)

// The daily people sync writes into USER.md, which the OS then reconciles against the enrollment store.
func TestHeartbeatPeopleSyncFormatMatchesTheReconciler(t *testing.T) {
	// Same expression as migratepersona.usersBlockRe (kept in sync deliberately: the two packages must not import each other for a prompt string).
	usersBlock := regexp.MustCompile(`(?i)^(?:Users:\s*)?\*\*([^*(]+?)\s*\([^)]*\)\*\*`)

	if !strings.Contains(heartbeatMDBlock, "- **<label> (friend)** — call: …; notes: …") {
		t.Fatal("the instruction no longer specifies the person-entry format")
	}
	for _, entry := range []string{
		"**long (friend)** — call: anh Long; notes: hunches at the screen",
		"Users: **long (friend)** — call: anh Long; notes: hunches",
		"**long (friend)**: prefers Vietnamese",
	} {
		m := usersBlock.FindStringSubmatch(entry)
		if m == nil {
			t.Errorf("reconciler cannot parse an entry in the taught format: %q", entry)
			continue
		}
		if m[1] != "long" {
			t.Errorf("label parsed as %q, want %q", m[1], "long")
		}
	}
}

// The constraints are the point of this block: without them the sync is exactly the mechanism that fused two users into one profile in the first place.
func TestHeartbeatPeopleSyncCarriesItsConstraints(t *testing.T) {
	for _, want := range []string{
		"Only write what you observed about THAT person",
		"never carry a former user's traits",
		"Never delete a PERSON's entry",
		"Do NOT fill",
	} {
		if !strings.Contains(heartbeatMDBlock, want) {
			t.Errorf("people sync lost its constraint: %q", want)
		}
	}
}

// The synthesis must NOT be gated on a wall-clock hour.
func TestHeartbeatSynthesisIsCatchUpNotClockGated(t *testing.T) {
	if strings.Contains(heartbeatMDBlock, "If current time is >= 21:00 AND") {
		t.Error("synthesis is gated on a fixed hour again — it will not fire on a device switched off in the evening")
	}
	for _, want := range []string{
		"catch-up",
		"every day BEFORE today", // never distil a day still in progress
	} {
		if !strings.Contains(heartbeatMDBlock, want) {
			t.Errorf("catch-up gate lost its wording: %q", want)
		}
	}
}
