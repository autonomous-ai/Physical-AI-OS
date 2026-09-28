package mqtthandler

import (
	"fmt"
	"strings"

	"go.autonomous.ai/os/system/schedule"
)

// Ops alerts for the "Scheduled" feature, modelled on the connector alerts
// (connector_handler.go: "✅ Connector <code> connected", "❌ connector.set
// <code> — FAILED"). One alert per event, for: a schedule.sync the cloud
// pushed, a task created or deleted on the device itself, and every run the
// Runner reports (ticker fire or "Run now").
//
// Data scope matches every other alertOps caller (see lib/alert's package
// doc): device-action metadata only. Task NAMES are included — they are what
// makes an alert actionable, and connector alerts already carry equivalent
// labels — but never a task's instructions, which are the user's own prompt
// text.

// scheduleSyncFailedTitle is the one title for every schedule.sync failure;
// the reason travels as the alert's detail.
const scheduleSyncFailedTitle = "❌ schedule.sync — FAILED"

// alertScheduleEvent is the single dispatch point for every schedule alert.
//
// ASYNCHRONOUS in production, unlike the plain alertOps most handlers call —
// and that difference is the point. alert.Notifyf is synchronous: it shells
// out (iwgetid, hostname -I) to build the device preamble and then POSTs with
// a 10s client timeout. The connector handlers get away with calling it
// inline because they already run on their own goroutine (runConnectorSet),
// after the ack. The schedule events do not: schedule.sync alerts sit on the
// MQTT dispatch path ahead of the ack, create/delete alerts ahead of the HTTP
// response, and run alerts on the Runner's report callback — i.e. on the
// scheduler tick itself, where a stuck alert endpoint would hold up every
// other due task. Best-effort must mean "can never delay the action" too, not
// just "errors are swallowed", so the send goes on its own goroutine. Nothing
// waits for it and nothing reads its outcome (alert.Notify logs and swallows
// every failure itself).
//
// scheduleAlert, when set (tests only), receives the alert synchronously
// instead, so a test can assert exactly which alerts one event produced.
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

// scheduleSyncAlertTitle summarises one applied schedule.sync:
//
//	✅ schedule.sync — applied 3 (created: Inbox digest, Standup; deleted: Old digest)
//
// applied is the size of the new list. created/deleted come from diffing ids
// between prior (the store's list before the replace) and next (the synced
// list): created in the sync's order, deleted in the store's order. Empty
// parts are omitted, and a sync that changed no ids — the common reconnect /
// drift-repair case, or a pure edit — is just "applied N". Edits to a
// surviving id are deliberately not itemised: which fields changed is not
// something the device can tell from a full-state replace, and a rename
// would otherwise read as a delete plus a create.
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
// "❌ Schedule create: <name> — FAILED", or without the name when the request
// never got far enough to have one (an unparseable body).
func scheduleOpFailedTitle(op, subject string) string {
	if subject = strings.TrimSpace(subject); subject == "" {
		return "❌ Schedule " + op + " — FAILED"
	}
	return "❌ Schedule " + op + ": " + subject + " — FAILED"
}

// scheduleRunAlert maps one RunReport — the Runner's report for EVERY run,
// ticker and "Run now" alike — onto its alert:
//
//	✅ Schedule ran: <name>
//	⏭️ Schedule skipped: <name> — missing connector: gmail
//	❌ Schedule run failed: <name>          (detail: the error)
//
// with " (run now)" after the name for a manual run (RunReport.Manual). The
// skip's reason goes in the title because it IS the whole story (which
// connector to set up); a failure's error text can be long, so it goes in the
// detail. Any status other than success/skipped is reported as a failure —
// "failure" is the only other status the Runner produces.
//
// Because this is driven by the report callback, it inherits the Runner's ack
// discipline for free: a send failure retried every tick for one occurrence
// alerts once, exactly as it acks once (see Runner.fire).
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
