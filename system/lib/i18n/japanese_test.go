package i18n

import (
	"reflect"
	"strings"
	"testing"
	"unicode"
)

func TestJapaneseLanguageNormalization(t *testing.T) {
	for _, code := range []string{"ja", "ja-JP", "ja_JP", " JA-jp "} {
		if got := NormalizeLang(code); got != LangJA {
			t.Errorf("NormalizeLang(%q) = %q", code, got)
		}
	}
}

func TestJapanesePhraseCoverage(t *testing.T) {
	for phrase, langs := range phrases {
		pool := langs[LangJA]
		if len(pool) == 0 {
			t.Errorf("%s has no Japanese variants", phrase)
		}
		for _, text := range pool {
			if !strings.ContainsFunc(text, func(r rune) bool { return unicode.In(r, unicode.Hiragana, unicode.Katakana, unicode.Han) }) {
				t.Errorf("%s has non-Japanese variant %q", phrase, text)
			}
		}
		if !reflect.DeepEqual(poolFor(phrase, "ja-JP"), pool) {
			t.Errorf("%s regional Japanese falls back", phrase)
		}
	}
	for _, phrase := range ChitchatPhrases() {
		if len(InputPhrases(phrase)[LangJA]) == 0 {
			t.Errorf("%s has no Japanese inputs", phrase)
		}
	}
	for key := range toolFillers[LangEN] {
		if len(toolFillers[LangJA][key]) == 0 {
			t.Errorf("%s has no Japanese tool filler", key)
		}
		if !reflect.DeepEqual(FillerForTool("ja-JP", key), FillerForTool(LangJA, key)) {
			t.Errorf("%s regional filler falls back", key)
		}
	}
	for _, pool := range []func(string) []string{FillerOpening, FillerRealtime, FillerContinuation} {
		if !reflect.DeepEqual(pool("ja-JP"), pool(LangJA)) {
			t.Error("regional filler pool falls back")
		}
	}
}

func TestJapaneseDeviceNamePreservesUnicode(t *testing.T) {
	deviceNameMu.RLock()
	previousLower, previousDisplay := deviceNameLower, deviceNameDisplay
	deviceNameMu.RUnlock()
	previousWake := ChitchatWakeWords()
	t.Cleanup(func() {
		deviceNameMu.Lock()
		deviceNameLower, deviceNameDisplay = previousLower, previousDisplay
		deviceNameMu.Unlock()
		SetChitchatWakeWords(previousWake)
	})
	SetDeviceName("さくら")
	if got := applyName("{Name}は{name}です。"); got != "さくらはさくらです。" {
		t.Fatalf("name corrupted: %q", got)
	}
}
