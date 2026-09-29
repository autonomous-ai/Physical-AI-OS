package sensingmsg

import "sync"

var environmentReplay struct {
	sync.RWMutex
	allowed func() bool
}

// SetEnvironmentReplayAllowed sets the environment replay policy used by runtime queues.
func SetEnvironmentReplayAllowed(allowed func() bool) {
	environmentReplay.Lock()
	environmentReplay.allowed = allowed
	environmentReplay.Unlock()
}

// ReplayAllowed rechecks environment policy for a queued event; false when unconfigured.
func ReplayAllowed(eventType string) bool {
	if eventType != "environment.update" {
		return true
	}
	environmentReplay.RLock()
	allowed := environmentReplay.allowed
	environmentReplay.RUnlock()
	return allowed != nil && allowed()
}
