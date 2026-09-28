// Audio rules — volume, speaker mute, music stop, TTS interrupt.
package intent

import (
	"fmt"
	"log/slog"
	"sync"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/hal"
)

// Serialize intent read-modify-write pairs so simultaneous requests do not lose a step.
var volumeAdjustmentMu sync.Mutex

func adjustVolume(increase bool) *Result {
	if !volumeAdjustmentMu.TryLock() {
		return &Result{ExecutionFailed: true, TTSText: "I couldn't change that setting because another adjustment is in progress. Please try again."}
	}
	defer volumeAdjustmentMu.Unlock()
	result := &Result{Actions: []string{"GET /audio/volume"}}
	current, ceiling, err := hal.GetVolume()
	if err != nil {
		slog.Warn("intent volume read failed", "error", err)
		result.ExecutionFailed = true
		result.TTSText = "I couldn't read the speaker volume. Please try again."
		return result
	}
	next := current / 2
	result.TTSText = "Volume down!"
	if increase {
		step := max(1, ceiling/10)
		next = min(ceiling, current+step)
		result.TTSText = "Volume up!"
		if current >= ceiling {
			result.TTSText = "The speaker is already at its allowed maximum volume."
			return result
		}
	} else if current == 0 {
		result.TTSText = "The speaker volume is already zero."
		return result
	}
	body := fmt.Sprintf(`{"volume":%d}`, next)
	result.Actions = append(result.Actions, "POST /audio/volume "+body)
	if err := post("/audio/volume", body); err != nil {
		result.ExecutionFailed = true
		result.TTSText = "I couldn't change the speaker volume. Please try again."
		return result
	}
	result.Actions = append(result.Actions, "GET /audio/volume")
	applied, _, err := hal.GetVolume()
	if err != nil || applied != next {
		slog.Warn("intent volume verification failed", "expected", next, "actual", applied, "error", err)
		result.ExecutionFailed = true
		result.TTSText = "I couldn't confirm the speaker volume changed. Please try again."
	}
	return result
}

var audioRules = []rule{
	// --- Volume ---
	{
		name:       "volume_up",
		capability: device.CapAudio,
		match:      anyOf("volume up", "louder"),
		exec:       func(string) *Result { return adjustVolume(true) },
	},
	{
		name:       "volume_down",
		capability: device.CapAudio,
		match:      anyOf("volume down", "quieter"),
		exec:       func(string) *Result { return adjustVolume(false) },
	},
	// unmute before mute: belt-and-braces ordering on top of the
	// word-boundary match (containsPhrase) that already keeps "unmute
	// speaker" from hitting the mute keywords.
	{
		name:       "unmute_speaker",
		capability: device.CapMedia,
		match:      anyOf("unmute speaker", "unmute the speaker"),
		exec: func(string) *Result {
			executionFailed := post("/speaker/unmute", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Speaker on!", Actions: []string{`POST /speaker/unmute`}}
		},
	},
	{
		name:       "mute_speaker",
		capability: device.CapMedia,
		match:      anyOf("mute speaker", "mute the speaker"),
		exec: func(string) *Result {
			executionFailed := post("/speaker/mute", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "", Actions: []string{`POST /speaker/mute`}}
		},
	},

	// --- Music control ---
	{
		name:       "music_stop",
		capability: device.CapMedia,
		match:      anyOf("stop music", "stop the music", "music off", "stop playing"),
		exec: func(string) *Result {
			executionFailed := post("/audio/stop", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "Music stopped.", Actions: []string{"POST /audio/stop"}}
		},
	},

	// --- TTS stop (interrupt the device speaking) ---
	{
		name:       "stop_talking",
		capability: device.CapAudio,
		match:      anyOf("stop talking", "ok stop"),
		exec: func(string) *Result {
			executionFailed := post("/tts/stop", "") != nil
			return &Result{ExecutionFailed: executionFailed, TTSText: "", Actions: []string{"POST /tts/stop"}}
		},
	},
}
