package http

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/domain"
)

type unavailableHALTransport struct{}

func (unavailableHALTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("HAL unavailable")
}

func TestGetVoicesWhenHALUnavailable(t *testing.T) {
	// HAL's client uses the default transport. Restore it before parallel tests resume.
	original := http.DefaultTransport
	http.DefaultTransport = unavailableHALTransport{}
	t.Cleanup(func() { http.DefaultTransport = original })
	japanese := []string{"Shizuka", "Konoha", "Rin", "Asahi", "Hinata", "Hiroki"}
	english := []string{"Rachel", "Sarah", "Nicole", "Terra", "Maria", "Sophie", "Piper", "Mia", "Kimmy", "Brianna", "Ally", "Tori", "Brian", "Adam", "Daniel", "George", "James", "Liam", "Charlie", "Sam", "Sean", "Kael", "Brooks", "Erion"}
	for _, tc := range []struct {
		provider, lang string
		want           []string
		status         int
	}{
		{"elevenlabs", "en", english, http.StatusOK},
		{"elevenlabs", "en-US", english, http.StatusOK},
		{"elevenlabs", "fr", english, http.StatusOK},
		{"elevenlabs", "ja", japanese, http.StatusOK},
		{"elevenlabs", "ja-JP", japanese, http.StatusOK},
		{"elevenlabs", "ja_JP", japanese, http.StatusOK},
		{"elevenlabs", "JA-jp", japanese, http.StatusOK},
		{"elevenlabs", "vi-VN", []string{"Ngan", "Linh", "Huyen", "Freya", "Nathan", "Quan"}, http.StatusOK},
		{"elevenlabs", "zh-TW", []string{"Amy", "Sage", "Xiaoxi", "Yun", "Evan Zhao", "Jin"}, http.StatusOK},
		{"openai", "ja", domain.TTSVoicesByProvider[domain.TTSProviderOpenAI], http.StatusOK},
		{"gemini", "ja", domain.TTSVoicesByProvider[domain.TTSProviderGemini], http.StatusOK},
		{"unknown", "ja", domain.TTSVoices, http.StatusOK},
		{"piper", "ja", nil, http.StatusServiceUnavailable},
	} {
		t.Run(tc.provider+"/"+tc.lang, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest(http.MethodGet, "/device/voices?"+url.Values{"provider": {tc.provider}, "lang": {tc.lang}}.Encode(), nil)
			(&DeviceHandler{}).GetVoices(c)
			if w.Code != tc.status {
				t.Fatalf("status = %d, body = %s", w.Code, w.Body.String())
			}
			if tc.status != http.StatusOK {
				return
			}
			var response struct {
				Status int      `json:"status"`
				Data   []string `json:"data"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response.Status != 1 || !reflect.DeepEqual(response.Data, tc.want) {
				t.Fatalf("response = %+v, want voices %v", response, tc.want)
			}
		})
	}
}
