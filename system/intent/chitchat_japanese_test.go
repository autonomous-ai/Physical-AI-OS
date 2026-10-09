package intent

import (
	"net/http"
	"sync/atomic"
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
)

func TestJapaneseChitchatRequiresCompletePhrase(t *testing.T) {
	var emotionCalls atomic.Int32
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/emotion" {
			t.Errorf("unexpected HAL call: %s %s", r.Method, r.URL.Path)
		}
		emotionCalls.Add(1)
	})
	previousEnabled := chitchatEnabled()
	SetChitchatEnabled(true)
	t.Cleanup(func() { SetChitchatEnabled(previousEnabled) })
	previousWakeWords := i18n.ChitchatWakeWords()
	i18n.SetChitchatWakeWords([]string{"さくら"})
	t.Cleanup(func() { i18n.SetChitchatWakeWords(previousWakeWords) })

	for _, tc := range []struct {
		text string
		rule string
	}{
		{"いる？", "presence_check"},
		{"いる", "presence_check"},
		{"そこにいる？", "presence_check"},
		{"まだいる?", "presence_check"},
		{"聞こえる？", "presence_check"},
		{"さくら, いる？", "presence_check"},
		{"[user] Unknown Speaker: [voice:v1] いる？ (audio saved at /tmp/x.wav)", "presence_check"},
		{"こんにちは！", "greeting"},
		{"おはようございます。", "greeting"},
		{"ありがとうございます！", "thanks"},
		{"すみません", "apology"},
		{"かわいい", "compliment"},
		{"またね", "farewell"},
		{"気にしないで", "nevermind"},
		{"何している？", ""},
		{"誰がいる？", ""},
		{"ここにいる動物は？", ""},
		{"さくら, 何している？", ""},
		{"何が聞こえる？", ""},
		{"かわいい動物は？", ""},
		{"ありがとうの意味は？", ""},
	} {
		t.Run(tc.text, func(t *testing.T) {
			before := emotionCalls.Load()
			result := Match(tc.text)
			if tc.rule == "" {
				if result != nil {
					t.Errorf("Match(%q) = %+v, want agent fallback", tc.text, result)
				}
				if emotionCalls.Load() != before {
					t.Error("agent fallback must not send an emotion to HAL")
				}
				return
			}
			if result == nil || result.Rule != "chitchat_"+tc.rule || result.TTSText == "" || result.ExecutionFailed {
				t.Fatalf("Match(%q) = %+v, want successful chitchat_%s", tc.text, result, tc.rule)
			}
			if emotionCalls.Load() != before+1 {
				t.Error("matched chitchat must send exactly one emotion to HAL")
			}
		})
	}
}
