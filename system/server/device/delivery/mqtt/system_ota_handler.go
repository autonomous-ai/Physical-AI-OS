package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/ota"
	"go.autonomous.ai/os/system/server/config"
)

// softwareUpdateWatchTimeout bounds the completion watcher of one
// system.software_update.
const softwareUpdateWatchTimeout = 40 * time.Minute

// softwareUpdateWatchers holds the resolved targets that already have a
// completion watcher, so a re-trigger after the rate-limit window does not
// start a second one (the first will report for both).
var softwareUpdateWatchers sync.Map // key: resolved target

// handleSystemOTAVersions is the cloud twin of the web Versions card:
// bootstrap's per-component version report (incl. the "agent" alias) plus
// what it is installing right now.
func (h *DeviceMQTTHandler) handleSystemOTAVersions(env domain.MQTTDataCommand) error {
	status, errMsg, data := buildOTAVersionsReply(context.Background(), h.config)
	return h.publishDataResult(env.Kind, status, errMsg, data)
}

func buildOTAVersionsReply(ctx context.Context, cfg *config.Config) (string, string, map[string]any) {
	versions, err := ota.Versions(ctx, cfg)
	if err != nil {
		slog.Warn("system.ota_versions: versions failed", "component", "mqtt", "error", err)
		return "failure", err.Error(), nil
	}
	updating, err := ota.Updating(ctx, cfg)
	if err != nil {
		slog.Warn("system.ota_versions: updating failed", "component", "mqtt", "error", err)
		return "failure", err.Error(), nil
	}
	return "success", "", map[string]any{"versions": versions, "updating": updating}
}

// handleSystemSoftwareUpdate is the cloud twin of the Versions card's
// `update` button (POST /api/system/software-update/:target) — same
// ota.TriggerUpdate, same allowlist and same shared per-target rate limit.
func (h *DeviceMQTTHandler) handleSystemSoftwareUpdate(env domain.MQTTDataCommand) error {
	var req domain.MQTTSoftwareUpdateData
	if len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &req); err != nil {
			return h.publishDataResult(env.Kind, "failure", "invalid system.software_update data: "+err.Error(), nil)
		}
	}
	req.Target = strings.TrimSpace(req.Target)
	if req.Target == "" {
		return h.publishDataResult(env.Kind, "failure", "target is required", map[string]any{"target": ""})
	}

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	resolved, err := ota.TriggerUpdate(ctx, h.config, req.Target)
	cancel()
	status, errMsg, data := softwareUpdateAck(req.Target, resolved, err)
	if err != nil {
		slog.Warn("system.software_update rejected", "component", "mqtt", "target", req.Target, "resolved", resolved, "error", err)
		go h.alertOps(softwareUpdateAlert("rejected", req.Target, resolved, err.Error()))
		return h.publishDataResult(env.Kind, status, errMsg, data)
	}
	slog.Info("system.software_update started", "component", "mqtt", "target", req.Target, "resolved", resolved)
	if pubErr := h.publishDataResult(env.Kind, status, errMsg, data); pubErr != nil {
		slog.Error("system.software_update: ack publish failed", "component", "mqtt", "error", pubErr)
	}

	if _, running := softwareUpdateWatchers.LoadOrStore(resolved, struct{}{}); !running {
		go h.watchSoftwareUpdate(req.Target, resolved)
	}
	return nil
}

// softwareUpdateAck builds the immediate system.software_update reply.
func softwareUpdateAck(requested, resolved string, err error) (string, string, map[string]any) {
	if err != nil {
		data := map[string]any{"target": requested}
		var rl *ota.RateLimitedError
		if errors.As(err, &rl) {
			data["retry_after_seconds"] = rl.RetryAfterSeconds()
		}
		return "failure", err.Error(), data
	}
	return "success", "", map[string]any{
		"target":          requested,
		"resolved_target": resolved,
		"state":           "started",
	}
}

// watchSoftwareUpdate waits for bootstrap to finish installing resolved, then
// publishes the unsolicited completion report.
func (h *DeviceMQTTHandler) watchSoftwareUpdate(requested, resolved string) {
	defer softwareUpdateWatchers.Delete(resolved)
	started := time.Now()
	h.alertSoftwareUpdateStarted(requested, resolved)
	ctx, cancel := context.WithTimeout(context.Background(), softwareUpdateWatchTimeout)
	defer cancel()

	waitErr := ota.WaitUntilDone(ctx, h.config, resolved, ota.WaitOptions{})
	var versions map[string]any
	var verr error
	if waitErr == nil {
		for attempt := 0; attempt < 5; attempt++ {
			if versions, verr = ota.Versions(context.Background(), h.config); verr == nil {
				break
			}
			time.Sleep(3 * time.Second)
		}
	}
	status, errMsg, data := softwareUpdateCompletion(requested, resolved, waitErr, versions, verr)
	slog.Info("system.software_update finished", "component", "mqtt",
		"target", requested, "resolved", resolved, "status", status, "error", errMsg)
	if err := h.publishDataResult(domain.KindSystemSoftwareUpdate, status, errMsg, data); err != nil {
		slog.Error("system.software_update: completion publish failed", "component", "mqtt", "error", err)
	}
	h.alertOps(softwareUpdateAlert(status, requested, resolved,
		softwareUpdateDoneDetail(status, errMsg, data, time.Since(started))))
}

// alertSoftwareUpdateStarted announces an accepted update with the version it
// moves from and to (omitted when bootstrap's report is unreadable).
func (h *DeviceMQTTHandler) alertSoftwareUpdateStarted(requested, resolved string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	versions, err := ota.Versions(ctx, h.config)
	if err != nil {
		versions = nil
	}
	h.alertOps(softwareUpdateAlert("started", requested, resolved, softwareUpdateStartDetail(versions, resolved)))
}

// softwareUpdateAlert builds the ops alert for one stage of an update requested
// over MQTT, so an install started remotely is visible from start to finish.
// stage: "started", "rejected", "success" or "failure".
func softwareUpdateAlert(stage, requested, resolved, detail string) (string, string) {
	label := requested
	if resolved != "" && resolved != requested {
		label = requested + " (" + resolved + ")"
	}
	switch stage {
	case "started":
		return "⬆️ Software update " + label + " — started", detail
	case "rejected":
		return "❌ Software update " + label + " — rejected", detail
	case "success":
		return "✅ Software update " + label + " — done", detail
	default:
		return "❌ Software update " + label + " — failed", detail
	}
}

// softwareUpdateStartDetail is "<current> → <published>", or "" when unknown.
func softwareUpdateStartDetail(versions map[string]any, resolved string) string {
	cv, ok := ota.Component(versions, resolved)
	if !ok || cv.Current == "" {
		return ""
	}
	return cv.Current + " → " + cv.Target
}

// softwareUpdateDoneDetail reports the installed version (or the failure) and
// how long the update took.
func softwareUpdateDoneDetail(status, errMsg string, data map[string]any, took time.Duration) string {
	d := took.Round(time.Second).String()
	if status == "success" {
		if current, _ := data["current"].(string); current != "" {
			return "now " + current + " · took " + d
		}
		return "took " + d
	}
	return errMsg + " · after " + d
}

// softwareUpdateCompletion builds the unsolicited completion report.
func softwareUpdateCompletion(requested, resolved string, waitErr error, versions map[string]any, verr error) (string, string, map[string]any) {
	data := map[string]any{
		"target":          requested,
		"resolved_target": resolved,
		"state":           "failed",
	}
	if waitErr != nil {
		return "failure", "update did not finish: " + waitErr.Error(), data
	}
	if verr != nil {
		return "failure", "update finished but versions are unreadable: " + verr.Error(), data
	}
	cv, ok := ota.Component(versions, resolved)
	if !ok {
		return "failure", "update finished but " + resolved + " is not in the version report", data
	}
	data["current"] = cv.Current
	data["target_version"] = cv.Target
	data["update_available"] = cv.UpdateAvailable
	if cv.UpdateAvailable {
		return "failure", "update finished but " + resolved + " is still at " + cv.Current + " (published " + cv.Target + ")", data
	}
	data["state"] = "completed"
	return "success", "", data
}
