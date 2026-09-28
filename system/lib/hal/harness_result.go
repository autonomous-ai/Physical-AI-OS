package hal

import "encoding/json"

// Harness update kinds accepted by HAL's announcer.
const (
	HarnessUpdateResult   = "result"
	HarnessUpdateQuestion = "question"
	HarnessUpdateProgress = "progress"
)

// harnessUpdateMaxRunes stays under HAL's request limit; the announcer cuts
// the text much shorter before rendering it anyway.
const harnessUpdateMaxRunes = 16000

// AnnounceHarnessUpdate queues raw Harness text with HAL's announcer, which
// speaks a rendered version (realtime model or summarizer) once the device is
// free. Nil means HAL queued it, never proof of playback; ErrSpeakerMuted when
// HAL suppressed it. turnID owns the speech for cancellation.
func AnnounceHarnessUpdate(kind, text, turnID, outcome string) error {
	if runes := []rune(text); len(runes) > harnessUpdateMaxRunes {
		text = string(runes[:harnessUpdateMaxRunes])
	}
	body, _ := json.Marshal(map[string]any{"kind": kind, "text": text, "turn_id": turnID, "outcome": outcome})
	return postSpeak("/voice/harness/update", body)
}
