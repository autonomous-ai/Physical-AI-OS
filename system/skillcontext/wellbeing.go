// Package skillcontext builds pre-fetched context blocks the sensing handler
// injects into the agent message, saving the agent a read tool turn.
package skillcontext

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/usercanon"
	"go.autonomous.ai/os/system/skillcontext/posture"
	"go.autonomous.ai/os/system/skillcontext/wellbeing"
)

const (
	usersDir         = "/root/local/users"
	patternsSubpath  = "habit/patterns.json"
	wellbeingSubdir  = "wellbeing"
	patternsFreshAge = 6 * time.Hour
	bootstrapMinDays = 3
)

// reactionCountActions are the user-driven actions counted per day.
var reactionCountActions = []string{"drink", "break"}

// nonActivityActions are wellbeing rows that don't count as user activity
// (presence markers, agent nudges, yawning).
var nonActivityActions = map[string]bool{
	"enter":            true,
	"leave":            true,
	"yawning":          true,
	"nudge_hydration":  true,
	"nudge_break":      true,
	"nudge_toilet":     true,
	"noted_yawn":       true,
	"morning_greeting": true,
	"sleep_winddown":   true,
	"meal_reminder":    true,
}

// eatLabels are raw Kinetics labels HAL emits for the eat bucket.
var eatLabels = map[string]bool{
	"tasting food":      true,
	"dining":            true,
	"eating burger":     true,
	"eating cake":       true,
	"eating carrots":    true,
	"eating doughnuts":  true,
	"eating hotdog":     true,
	"eating ice cream":  true,
	"eating spaghetti":  true,
	"eating watermelon": true,
}

// Lunch / dinner meal-reminder windows.
const (
	lunchWindowStartHour  = 11 // 11:30 — start offset applied in inMealWindow
	lunchWindowEndHour    = 13 // 13:30
	dinnerWindowStartHour = 18 // 18:30
	dinnerWindowEndHour   = 20 // 20:30
	morningEndHour        = 11 // morning greeting fires on first activity 5-11h
	morningStartHour      = 5
	sleepWinddownHour     = 21 // sedentary at >=21h routes to sleep wind-down
)

// wellbeingContext is the digest the agent reads; deltas are pre-computed.
type wellbeingContext struct {
	HydrationDeltaMin        int                      `json:"hydration_delta_min"`         // minutes since last drink/enter/nudge_hydration; -1 if no reset today
	BreakDeltaMin            int                      `json:"break_delta_min"`             // minutes since last break/enter/nudge_break; falls back to first real activity today; -1 if no rows at all
	LatestActivity           string                   `json:"latest_activity"`             // most recent action label (sedentary or reset); "" if no events today
	CountToday               map[string]int           `json:"count_today,omitempty"`       // count of reset actions today (drink, break); zeros omitted
	TimeOfDay                string                   `json:"time_of_day"`                 // morning|noon|afternoon|evening|night — flavors reaction phrasing
	CurrentHour              int                      `json:"current_hour"`                // exact hour (0-23) for routing — finer than time_of_day
	FirstActivityToday       bool                     `json:"first_activity_today"`        // true when no wellbeing events logged yet today (this event is the first)
	MealWindow               string                   `json:"meal_window,omitempty"`       // "lunch" | "dinner" | "" — set when current_hour is inside a meal window
	MealSignalInWindow       bool                     `json:"meal_signal_in_window"`       // true when a meal signal (meal_reminder log OR any raw eat label) was already logged in the current window today — gates meal-reminder so the agent doesn't ask "ăn chưa?" after a real meal
	MorningGreetingDoneToday bool                     `json:"morning_greeting_done_today"` // true when a morning_greeting action exists today
	SleepWinddownDoneToday   bool                     `json:"sleep_winddown_done_today"`   // true when a sleep_winddown action exists today
	YawnAckAgeMin            int                      `json:"yawn_ack_age_min"`            // minutes since the agent last acknowledged a yawn (noted_yawn); -1 if never today — gates the yawn reaction to once an hour
	DrinksSinceToiletNudge   int                      `json:"drinks_since_toilet_nudge"`   // count of `drink` rows logged after the most recent `nudge_toilet` today (or all today's drinks if none); resets via nudge_toilet POST
	Patterns                 map[string]patternDigest `json:"patterns,omitempty"`          // wellbeing_patterns from patterns.json, keyed by action ("drink"/"break")
	BootstrapNeeded          bool                     `json:"bootstrap_needed"`            // patterns missing/stale AND days >= 3 → invoke habit Flow A only when nudging
	LastPostureNudgeAgeMin   int                      `json:"last_posture_nudge_age_min"`  // minutes since last nudge_posture today; -1 if none. Lets the skill defend against double-nudging if hal lost its cooldown state (restart), and supports the praise route (recent nudge + improving summary -> praise instead of re-nudge).
}

type patternDigest struct {
	TypicalHour   int    `json:"typical_hour"`
	TypicalMinute int    `json:"typical_minute,omitempty"`
	Strength      string `json:"strength"`
}

// BuildWellbeingContext returns a `[wellbeing_context: ...]` block for motion.activity,
// or "" on hard failure so the SKILL.md fallback runs.
func BuildWellbeingContext(user string) string {
	user = usercanon.Resolve(user)
	if user == "" {
		user = "unknown"
	}
	now := time.Now()
	today := now.Format("2006-01-02")

	events := wellbeing.Query(user, today, 0)
	yawnAckAge := computeDeltaMin(events, now, []string{"noted_yawn"})
	hydrationDelta := computeDeltaMin(events, now, []string{"drink", "enter", "nudge_hydration"})
	breakDelta := computeDeltaMin(events, now, []string{"break", "enter", "nudge_break"})
	if breakDelta == -1 {
		// No reset point today: count from the first real activity so the break nudge can fire.
		breakDelta = minutesSinceFirstActivity(events, now)
	}
	latestActivity := latestAction(events)
	countToday := countTodayActions(events, reactionCountActions)
	timeOfDay := timeOfDayLabel(now)
	currentHour := now.Hour()
	firstActivityToday := isFirstActivityToday(events, now)
	mealWindow := mealWindowFor(now)
	mealSignalInWindow := hasMealSignalInWindow(events, mealWindow, now)
	morningGreetingDone := hasActionToday(events, "morning_greeting")
	sleepWinddownDone := hasActionToday(events, "sleep_winddown")
	drinksSinceToiletNudge := countDrinksSinceToiletNudge(events)

	patterns, patternsFresh := readWellbeingPatterns(user)
	days := countWellbeingDays(user)
	bootstrapNeeded := !patternsFresh && days >= bootstrapMinDays

	postureEvents := posture.Query(user, today, 0)
	lastPostureNudgeAge := lastPostureNudgeAgeMin(postureEvents, now)

	ctx := wellbeingContext{
		HydrationDeltaMin:        hydrationDelta,
		BreakDeltaMin:            breakDelta,
		LatestActivity:           latestActivity,
		CountToday:               countToday,
		TimeOfDay:                timeOfDay,
		CurrentHour:              currentHour,
		FirstActivityToday:       firstActivityToday,
		MealWindow:               mealWindow,
		MealSignalInWindow:       mealSignalInWindow,
		MorningGreetingDoneToday: morningGreetingDone,
		SleepWinddownDoneToday:   sleepWinddownDone,
		YawnAckAgeMin:            yawnAckAge,
		DrinksSinceToiletNudge:   drinksSinceToiletNudge,
		Patterns:                 patterns,
		BootstrapNeeded:          bootstrapNeeded,
		LastPostureNudgeAgeMin:   lastPostureNudgeAge,
	}

	body, err := json.Marshal(ctx)
	if err != nil {
		slog.Warn("wellbeing context: marshal failed", "component", "skillcontext", "error", err)
		return ""
	}
	return fmt.Sprintf("\n[wellbeing_context: %s]", string(body))
}

// lastPostureNudgeAgeMin returns minutes since today's last nudge_posture row, or -1.
func lastPostureNudgeAgeMin(events []posture.Event, now time.Time) int {
	var latestTS float64
	for _, e := range events {
		if e.Action != posture.ActionNudge {
			continue
		}
		if e.TS > latestTS {
			latestTS = e.TS
		}
	}
	if latestTS == 0 {
		return -1
	}
	return int(now.Sub(time.Unix(int64(latestTS), 0)).Minutes())
}

// computeDeltaMin returns minutes since the latest event in resetActions, or -1 if none today.
func computeDeltaMin(events []wellbeing.Event, now time.Time, resetActions []string) int {
	var latestTS float64
	for _, e := range events {
		if !contains(resetActions, e.Action) {
			continue
		}
		if e.TS > latestTS {
			latestTS = e.TS
		}
	}
	if latestTS == 0 {
		return -1
	}
	return int(now.Sub(time.Unix(int64(latestTS), 0)).Minutes())
}

// minutesSinceFirstActivity returns minutes since today's first real activity, or -1.
func minutesSinceFirstActivity(events []wellbeing.Event, now time.Time) int {
	for _, e := range events {
		if nonActivityActions[e.Action] {
			continue
		}
		return int(now.Sub(time.Unix(int64(e.TS), 0)).Minutes())
	}
	return -1
}

// latestAction returns the action label of today's most recent event.
func latestAction(events []wellbeing.Event) string {
	if len(events) == 0 {
		return ""
	}
	return events[len(events)-1].Action
}

// countTodayActions tallies today's actions, dropping zero entries.
func countTodayActions(events []wellbeing.Event, actions []string) map[string]int {
	counts := make(map[string]int, len(actions))
	for _, a := range actions {
		counts[a] = 0
	}
	for _, e := range events {
		if _, ok := counts[e.Action]; ok {
			counts[e.Action]++
		}
	}
	for k, v := range counts {
		if v == 0 {
			delete(counts, k)
		}
	}
	if len(counts) == 0 {
		return nil
	}
	return counts
}

// timeOfDayLabel buckets the hour into a coarse phrase.
func timeOfDayLabel(now time.Time) string {
	switch h := now.Hour(); {
	case h >= 5 && h < 11:
		return "morning"
	case h >= 11 && h < 13:
		return "noon"
	case h >= 13 && h < 18:
		return "afternoon"
	case h >= 18 && h < 22:
		return "evening"
	default:
		return "night"
	}
}

// mealWindowFor returns "lunch" (11:30-13:30), "dinner" (18:30-20:30) or "".
func mealWindowFor(now time.Time) string {
	mins := now.Hour()*60 + now.Minute()
	switch {
	case mins >= lunchWindowStartHour*60+30 && mins < (lunchWindowEndHour+0)*60+30:
		return "lunch"
	case mins >= dinnerWindowStartHour*60+30 && mins < (dinnerWindowEndHour+0)*60+30:
		return "dinner"
	default:
		return ""
	}
}

// hasMealSignalInWindow reports whether a meal reminder or eat label already occurred
// today in window.
func hasMealSignalInWindow(events []wellbeing.Event, window string, now time.Time) bool {
	if window == "" {
		return false
	}
	for _, e := range events {
		if e.Action != "meal_reminder" && !eatLabels[e.Action] {
			continue
		}
		ts := time.Unix(int64(e.TS), 0).In(now.Location())
		if mealWindowFor(ts) == window {
			return true
		}
	}
	return false
}

// hasActionToday reports whether today's events contain action.
func hasActionToday(events []wellbeing.Event, action string) bool {
	for _, e := range events {
		if e.Action == action {
			return true
		}
	}
	return false
}

// countDrinksSinceToiletNudge counts today's drink rows after the last nudge_toilet row.
func countDrinksSinceToiletNudge(events []wellbeing.Event) int {
	var lastToiletTS float64
	for _, e := range events {
		if e.Action == "nudge_toilet" && e.TS > lastToiletTS {
			lastToiletTS = e.TS
		}
	}
	count := 0
	for _, e := range events {
		if e.Action == "drink" && e.TS > lastToiletTS {
			count++
		}
	}
	return count
}

// isFirstActivityToday reports whether no prior real activity was logged today.
// HAL writes the row before firing the event, so rows from the last 5s are the current event.
func isFirstActivityToday(events []wellbeing.Event, now time.Time) bool {
	cutoff := now.Add(-5 * time.Second)
	for _, e := range events {
		if nonActivityActions[e.Action] {
			continue
		}
		ts := time.Unix(int64(e.TS), 0)
		if ts.Before(cutoff) {
			return false
		}
	}
	return true
}

func contains(haystack []string, needle string) bool {
	for _, s := range haystack {
		if s == needle {
			return true
		}
	}
	return false
}

// readWellbeingPatterns returns wellbeing patterns by action and whether the file is fresh.
func readWellbeingPatterns(user string) (map[string]patternDigest, bool) {
	path := filepath.Join(usersDir, user, patternsSubpath)
	info, err := os.Stat(path)
	if err != nil {
		return nil, false
	}
	if time.Since(info.ModTime()) >= patternsFreshAge {
		return nil, false
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, false
	}
	var raw struct {
		WellbeingPatterns []struct {
			Action        string `json:"action"`
			TypicalHour   int    `json:"typical_hour"`
			TypicalMinute int    `json:"typical_minute"`
			Strength      string `json:"strength"`
		} `json:"wellbeing_patterns"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, true // file is fresh but malformed; skip patterns, no need to bootstrap
	}
	out := make(map[string]patternDigest, len(raw.WellbeingPatterns))
	for _, p := range raw.WellbeingPatterns {
		// Only moderate/strong patterns; weak ones add noise.
		if p.Strength != "moderate" && p.Strength != "strong" {
			continue
		}
		out[strings.ToLower(p.Action)] = patternDigest{
			TypicalHour:   p.TypicalHour,
			TypicalMinute: p.TypicalMinute,
			Strength:      p.Strength,
		}
	}
	return out, true
}

// countWellbeingDays counts daily wellbeing files (habit bootstrap needs >=3).
func countWellbeingDays(user string) int {
	dir := filepath.Join(usersDir, user, wellbeingSubdir)
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	n := 0
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), ".jsonl") {
			n++
		}
	}
	return n
}
