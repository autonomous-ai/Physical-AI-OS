package http

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// manualCaptureRunID retains the gateway unique ID while dating speech by capture,
// so a delayed older dispatch remains behind the physical supersede watermark.
func manualCaptureRunID(runID string, req SensingEventRequest, now time.Time) string {
	if req.HarnessVoice == nil || req.HarnessVoice.Enabled || req.CapturedAtMS <= 0 || req.CapturedAtMS > now.UnixMilli()+1 || req.CapturedAtMS < now.Add(-24*time.Hour).UnixMilli() {
		return runID
	}
	if req.Type != "voice_command" && req.Type != "voice_agent_handled" {
		return runID
	}
	if i := strings.LastIndex(runID, "-"); i >= 0 && len(runID[i+1:]) == 13 {
		if _, err := strconv.ParseInt(runID[i+1:], 10, 64); err == nil {
			return fmt.Sprintf("%s-%d", runID[:i], req.CapturedAtMS)
		}
	}
	return fmt.Sprintf("%s-%d", runID, req.CapturedAtMS)
}
