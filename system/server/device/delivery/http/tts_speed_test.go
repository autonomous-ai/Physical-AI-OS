package http

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestUpdateConfigRejectsInvalidTTSSpeed(t *testing.T) {
	for _, value := range []string{"0", "0.24", "4.01", `"1.2"`, "1e999"} {
		t.Run(value, func(t *testing.T) {
			w := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(w)
			c.Request = httptest.NewRequest(http.MethodPut, "/device/config", strings.NewReader(`{"tts_speed":`+value+`,"tts_voice":"must-not-save"}`))
			c.Request.Header.Set("Content-Type", "application/json")
			(&DeviceHandler{}).UpdateConfig(c)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d; body = %s", w.Code, w.Body.String())
			}
		})
	}
}
