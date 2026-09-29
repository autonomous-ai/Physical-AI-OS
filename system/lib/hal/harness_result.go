package hal

import "encoding/json"

// Harness update kinds accepted by HAL's announcer.
const (
	HarnessUpdateResult   = "result"
	HarnessUpdateQuestion = "question"
	HarnessUpdateProgress = "progress"
)

// harnessUpdateMaxRunes stays under HAL's request limit.
const harnessUpdateMaxRunes = 16000

// AnnounceHarnessUpdate queues raw Harness text with HAL's announcer; turnID owns the speech.
// Nil means queued (not played); ErrSpeakerMuted when HAL suppressed it.
func AnnounceHarnessUpdate(kind, text, turnID, outcome string) error {
	if runes := []rune(text); len(runes) > harnessUpdateMaxRunes {
		text = string(runes[:harnessUpdateMaxRunes])
	}
	body, _ := json.Marshal(map[string]any{"kind": kind, "text": text, "turn_id": turnID, "outcome": outcome})
	return postSpeak("/voice/harness/update", body)
}
