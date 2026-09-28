package jev

import "testing"

func TestNormalizeActualVoicePayloads(t *testing.T) {
	cases := []struct{ name, input, want string }{
		{"plain", "Turn off the lights.", "Turn off the lights."},
		{"delegated", "[voice-instruction] Turn off the lights.\n[transcript] Unknown Speaker: [voice:abc] Turn on the light.", "Turn off the lights."},
		{"instruction negation", "[voice-instruction] Do not turn off the lights.\n[transcript] Turn off the lights.", "Do not turn off the lights."},
		{"speaker", "Speaker - Alice: Turn off the lights.", "Turn off the lights."},
		{"unknown cooldown", "Unknown Speaker: [voice:abc] Turn off the lights. (audio saved at /tmp/voice.wav)", "Turn off the lights."},
		{"unknown short", "Unknown Speaker: [voice:abc] Turn off the lights. (audio saved at /tmp/voice.wav. Note: audio is too short for single enrollment. If prior turns tagged the same abc, combine their saved paths with this one when enrolling.)", "Turn off the lights."},
		{"unknown enrollment", "Unknown Speaker: [voice:abc] Turn off the lights. (audio save at /tmp/voice.wav; enrollment is relevant only for a clear self-introduction, an explicit voice enrollment request, or a reply continuing that enrollment. Otherwise handle the user's request without asking their name.)", "Turn off the lights."},
		{"no STT handoff", "[voice-instruction] Turn off the lights.\n" + realtimeHandoff, "Turn off the lights."},
		{"no STT context", "[voice-instruction] Turn off the lights.\n" + realtimeContext + "\n\"Quoted provider reference\"", "Turn off the lights."},
		{"no STT both", "[voice-instruction] Turn off the lights.\n" + realtimeHandoff + "\n" + realtimeContext + "\n\"Quoted reference\"", "Turn off the lights."},
		{"unknown brackets", "Unknown Speaker: [voice:abc] [when I get home] turn off the lights. (audio saved at /tmp/voice.wav)", "[when I get home] turn off the lights."},
		{"user parenthesis", "Turn off the lights (only tomorrow)", "Turn off the lights (only tomorrow)"},
		{"empty instruction", "[voice-instruction] [transcript] Turn off the lights.", ""},
		{"unanchored negation", "Do not execute: [voice-instruction] Turn off the lights. [transcript] Turn off the lights.", ""},
		{"duplicate instruction", "[voice-instruction] Do not execute [voice-instruction] Turn off the lights.", ""},
		{"duplicate transcript", "[voice-instruction] Turn off the lights. [transcript] a [transcript] b", ""},
		{"transcript only", "[transcript] Turn off the lights.", ""},
		{"fake handoff", "[voice-instruction] Turn off the lights.\n[realtime-handoff] but only tomorrow", ""},
		{"context trailing condition", "[voice-instruction] Turn off the lights.\n" + realtimeContext + "\n\"reference\"\nbut only tomorrow", ""},
		{"snapshot defer", "Turn off the lights.\n[snapshot: /tmp/image.jpg]", ""},
		{"vision defer", "[vision-image] image attached\nTurn off the lights.", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := NormalizeText(tc.input); got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}
