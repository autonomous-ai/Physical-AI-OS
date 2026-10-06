package domain

import "testing"

func TestDefaultElevenLabsVoiceForLang(t *testing.T) {
	cases := map[string]string{
		"vi":    "Ngan",
		"vi-VN": "Ngan",
		"ja":    "Shizuka",
		"ja-JP": "Shizuka",
		"zh-CN": "Amy",
		"zh-TW": "Amy",
		"zh":    "Amy",
		"en":    "Rachel",
		"en-US": "Rachel",
		"":      "Rachel",
		"fr":    "Rachel",
	}
	for lang, want := range cases {
		if got := DefaultElevenLabsVoiceForLang(lang); got != want {
			t.Errorf("DefaultElevenLabsVoiceForLang(%q) = %q, want %q", lang, got, want)
		}
	}
}

func TestIsValidTTSProvider(t *testing.T) {
	for _, ok := range []string{TTSProviderOpenAI, TTSProviderElevenLabs} {
		if !IsValidTTSProvider(ok) {
			t.Errorf("IsValidTTSProvider(%q) = false, want true", ok)
		}
	}
	for _, bad := range []string{"", "nova", "google", "OpenAI"} {
		if IsValidTTSProvider(bad) {
			t.Errorf("IsValidTTSProvider(%q) = true, want false", bad)
		}
	}
}
