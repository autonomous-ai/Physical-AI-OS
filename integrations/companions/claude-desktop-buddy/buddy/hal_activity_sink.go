package buddy

import (
	"fmt"
	"log"
)

// HALActivitySink implements httpapi.ActivitySink: logs each event and speaks `sound` via TTS.
type HALActivitySink struct {
	bridge *Bridge
}

func NewHALActivitySink(b *Bridge) HALActivitySink { return HALActivitySink{bridge: b} }

func (s HALActivitySink) Notify(level, title, subtitle string, sound bool) {
	log.Printf("[activity] notify level=%q title=%q subtitle=%q sound=%v", level, title, subtitle, sound)
	if !sound {
		return
	}
	text := title
	if text == "" {
		text = subtitle
	}
	s.bridge.speakTTS("Claude code " + text) // → HAL :5001 /voice/speak (cached); empty text is a no-op
}

func (s HALActivitySink) Usage(fiveHour, sevenDay int, reset5h, reset7d string, sound bool) {
	log.Printf("[activity] usage 5h=%d%% (reset %s) 7d=%d%% (reset %s) sound=%v",
		fiveHour, reset5h, sevenDay, reset7d, sound)
	if !sound {
		return
	}
	s.bridge.speakTTS(fmt.Sprintf("Claude code: "+"Usage %d percent", fiveHour))
}
