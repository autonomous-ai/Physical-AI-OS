package bootstrap

import (
	"strings"
	"testing"
)

func TestOTAUpdateStartPhraseJapanese(t *testing.T) {
	for _, lang := range []string{"ja", "ja-JP", "ja_JP", " JA-jp "} {
		if got := otaUpdateStartPhrase(lang); !strings.Contains(got, "デバイスを更新") {
			t.Errorf("otaUpdateStartPhrase(%q) = %q", lang, got)
		}
	}
}
