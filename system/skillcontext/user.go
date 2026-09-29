package skillcontext

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/usercanon"
)

const userInfoTimeout = 600 * time.Millisecond

// userInfo mirrors HAL's GET /user/info payload.
type userInfo struct {
	Name             string `json:"name"`
	IsFriend         bool   `json:"is_friend"`
	TelegramID       string `json:"telegram_id,omitempty"`
	TelegramUsername string `json:"telegram_username,omitempty"`
}

// BuildUserContext returns a `[user_info: {...}]` block, or "" when unknown or on failure.
func BuildUserContext(user string) string {
	user = usercanon.Resolve(user)
	if user == "" || user == "unknown" {
		return ""
	}
	client := &http.Client{Timeout: userInfoTimeout}
	resp, err := client.Get(hal.BaseURL + "/user/info?name=" + user)
	if err != nil {
		slog.Warn("user context: fetch failed", "component", "skillcontext", "user", user, "error", err)
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return ""
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil {
		return ""
	}
	var info userInfo
	if json.Unmarshal(body, &info) != nil {
		return ""
	}
	out, err := json.Marshal(info)
	if err != nil {
		return ""
	}
	return fmt.Sprintf("\n[user_info: %s]", string(out))
}
