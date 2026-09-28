package mqtthandler

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/schedule"
)

// Schedule alerts carry task names, never instructions (the user's prompt).

// scheduleSyncFailedTitle is the one title for every schedule.sync failure;
// the reason travels as the alert's detail.
const scheduleSyncFailedTitle = "❌ schedule.sync — FAILED"

// alertScheduleEvent is the single dispatch point for every schedule alert.
// Asynchronous in production so it never delays an ack or the scheduler tick.
func (h *DeviceMQTTHandler) alertScheduleEvent(title, detail string) {
	if h.scheduleAlert != nil {
		h.scheduleAlert(title, detail)
		return
	}
	go h.alertOps(title, detail)
}

// scheduleDisplayName is how an alert names a task: its name, or its id when
// the name is blank, so an alert never reads "Schedule ran: " with nothing
// after it.
func scheduleDisplayName(id, name string) string {
	if n := strings.TrimSpace(name); n != "" {
		return n
	}
	return id
}

// scheduleSyncAlertTitle summarises one applied schedule.sync, e.g.
// "✅ schedule.sync — applied 3 (created: Standup; deleted: Old digest)".
func scheduleSyncAlertTitle(applied int, prior, next []schedule.Schedule) string {
	priorIDs := make(map[string]bool, len(prior))
	for _, s := range prior {
		priorIDs[s.ID] = true
	}
	nextIDs := make(map[string]bool, len(next))
	var created []string
	for _, s := range next {
		nextIDs[s.ID] = true
		if !priorIDs[s.ID] {
			created = append(created, scheduleDisplayName(s.ID, s.Name))
		}
	}
	var deleted []string
	for _, s := range prior {
		if !nextIDs[s.ID] {
			deleted = append(deleted, scheduleDisplayName(s.ID, s.Name))
		}
	}

	title := fmt.Sprintf("✅ schedule.sync — applied %d", applied)
	var parts []string
	if len(created) > 0 {
		parts = append(parts, "created: "+strings.Join(created, ", "))
	}
	if len(deleted) > 0 {
		parts = append(parts, "deleted: "+strings.Join(deleted, ", "))
	}
	if len(parts) > 0 {
		title += " (" + strings.Join(parts, "; ") + ")"
	}
	return title
}

// scheduleOpFailedTitle is the failure title for a device-side create/delete:
// "❌ Schedule create: <name> — FAILED", or without the name when the
// request never got far enough to have one (an unparseable body).
func scheduleOpFailedTitle(op, subject string) string {
	if subject = strings.TrimSpace(subject); subject == "" {
		return "❌ Schedule " + op + " — FAILED"
	}
	return "❌ Schedule " + op + ": " + subject + " — FAILED"
}

// scheduleRunAlert maps one RunReport — the Runner's report for EVERY run,
// ticker and "Run now" alike — onto its alert.
func scheduleRunAlert(rr schedule.RunReport) (title, detail string) {
	name := scheduleDisplayName(rr.ScheduleID, rr.Name)
	if rr.Manual {
		name += " (run now)"
	}
	switch rr.Status {
	case "success":
		return "✅ Schedule ran: " + name, ""
	case schedule.RunStatusSkipped:
		return "⏭️ Schedule skipped: " + name + " — " + rr.Summary, ""
	default:
		return "❌ Schedule run failed: " + name, rr.Summary
	}
}
