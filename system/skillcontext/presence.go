package skillcontext

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/lib/usercanon"
	"go.autonomous.ai/os/system/skillcontext/wellbeing"
)

// presenceLookbackDays caps how far back the last `leave` row is searched.
const presenceLookbackDays = 3

// presenceContext is the digest the agent reads on presence.enter events.
type presenceContext struct {
	// LastLeaveAgeMin is minutes since the last leave row, or -1 if none in the window.
	LastLeaveAgeMin int `json:"last_leave_age_min"`
	// CurrentHour is the hour 0-23.
	CurrentHour int `json:"current_hour"`
}

// BuildPresenceContext returns a `[presence_context: ...]` block; "" for unknown users.
func BuildPresenceContext(user string) string {
	user = usercanon.Resolve(user)
	if user == "" || user == "unknown" {
		return ""
	}

	now := time.Now()
	leaveTS := wellbeing.LastActionTS(user, "leave", presenceLookbackDays)
	ageMin := -1
	if leaveTS > 0 {
		ageMin = int(now.Sub(time.Unix(int64(leaveTS), 0)).Minutes())
	}

	ctx := presenceContext{
		LastLeaveAgeMin: ageMin,
		CurrentHour:     now.Hour(),
	}
	body, err := json.Marshal(ctx)
	if err != nil {
		slog.Warn("presence context: marshal failed", "component", "skillcontext", "error", err)
		return ""
	}
	return fmt.Sprintf("\n[presence_context: %s]", string(body))
}
