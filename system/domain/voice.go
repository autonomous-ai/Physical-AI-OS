package domain

import (
	"strings"
	"sync"
	"time"
)

const (
	enrollNudgeCooldown = 5 * time.Minute
	enrollInstruction   = "\n[Speaker context: Unknown identity does not block the user's request. Use speaker-recognizer/SKILL.md only for a clear self-introduction, an explicit voice enrollment/management request, or a reply continuing that enrollment. Otherwise handle the request without asking for a name or running speaker tools.]"
)

var (
	lastEnrollNudge   time.Time
	lastEnrollNudgeMu sync.Mutex
)

// AppendEnrollNudge appends the enroll instruction to unknown-speaker messages with saved audio, rate-limited by cooldown.
func AppendEnrollNudge(msg string) string {
	if !strings.Contains(msg, "Unknown Speaker:") || !strings.Contains(msg, "audio save at") {
		return msg
	}

	lastEnrollNudgeMu.Lock()
	defer lastEnrollNudgeMu.Unlock()

	if time.Since(lastEnrollNudge) < enrollNudgeCooldown {
		return msg
	}
	lastEnrollNudge = time.Now()
	return msg + enrollInstruction
}
