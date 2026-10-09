package http

import (
	"strconv"
	"strings"
)

// Superseded includes turns that arrive after the physical tap's cancel request.
func (fm *FillerManager) Superseded(runID string) bool {
	i := strings.LastIndex(runID, "-")
	if i < 0 || len(runID[i+1:]) != 13 {
		return false
	}
	at, err := strconv.ParseInt(runID[i+1:], 10, 64)
	return err == nil && at <= fm.supersededBefore.Load()
}

func (fm *FillerManager) CancelBefore(beforeMS int64) {
	for {
		old := fm.supersededBefore.Load()
		if old >= beforeMS || fm.supersededBefore.CompareAndSwap(old, beforeMS) {
			break
		}
	}
	// HAL already stopped local playback; never issue a delayed global stop.
	fm.cancelMatching(fm.Superseded, false)
}
