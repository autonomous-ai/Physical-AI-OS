package hal

import (
	"sync"
	"time"
)

const turnSpeechPolicyTTL = time.Hour
const turnSpeechPolicyLimit = 4096

type turnSpeechPolicy struct {
	passive bool
	created time.Time
}

type turnSpeechPolicyRegistry struct {
	mu      sync.Mutex
	entries map[string]turnSpeechPolicy
}

var turnSpeechPolicies turnSpeechPolicyRegistry

// RegisterTurnSpeechPolicy preserves the sensing origin before asynchronous dispatch.
// The first registration wins: an internal retry may relabel a sensing turn as chat.
// Entries survive lifecycle completion for late streamed segments, bounded by one
// hour and 4096 runs; unknown or expired runs retain legacy speech admission.
func RegisterTurnSpeechPolicy(runID string, passive bool) {
	turnSpeechPolicies.register(runID, passive, time.Now())
}

// PassiveSensingTurn reports whether a known turn must yield to automatic input.
func PassiveSensingTurn(runID string) bool {
	return turnSpeechPolicies.passive(runID, time.Now())
}

func (r *turnSpeechPolicyRegistry) register(runID string, passive bool, now time.Time) {
	if runID == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.entries == nil {
		r.entries = make(map[string]turnSpeechPolicy)
	}
	for id, entry := range r.entries {
		if now.Sub(entry.created) >= turnSpeechPolicyTTL {
			delete(r.entries, id)
		}
	}
	if _, exists := r.entries[runID]; exists {
		return
	}
	if len(r.entries) >= turnSpeechPolicyLimit {
		var oldestID string
		var oldest time.Time
		for id, entry := range r.entries {
			if oldestID == "" || entry.created.Before(oldest) {
				oldestID, oldest = id, entry.created
			}
		}
		delete(r.entries, oldestID)
	}
	r.entries[runID] = turnSpeechPolicy{passive: passive, created: now}
}

func (r *turnSpeechPolicyRegistry) passive(runID string, now time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	entry, exists := r.entries[runID]
	if exists && now.Sub(entry.created) >= turnSpeechPolicyTTL {
		delete(r.entries, runID)
		return false
	}
	return entry.passive
}
