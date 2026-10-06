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

func TestElevenLabsVoicesAllLanguages(t *testing.T) {
	voices := ElevenLabsVoicesForLang("")
	if len(voices) != 42 {
		t.Fatalf("got %d voices, want all 42 curated voices", len(voices))
	}
	for index, want := range map[int]string{0: "Rachel", 24: "Ngan", 30: "Shizuka", 36: "Amy"} {
		if voices[index] != want {
			t.Errorf("voice[%d] = %q, want %q", index, voices[index], want)
		}
	}
	// Callers must not be able to mutate future responses or defaults.
	voices[0] = "changed"
	english := ElevenLabsVoicesForLang("en")
	english[0] = "changed"
	if got := DefaultElevenLabsVoiceForLang("en"); got != "Rachel" {
		t.Fatalf("default mutated: %q", got)
	}
}
