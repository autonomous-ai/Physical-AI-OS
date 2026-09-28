package device

import (
	"encoding/json"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestIMessageConfigPartialJSON(t *testing.T) {
	for _, tc := range []struct {
		name, payload, url, address, persona, password string
	}{
		{"omitted", `{"llm_model":"new-model"}`, "http://bridge", "owner@example.com", "saved persona", "saved-password"},
		{"null", `{"bluebubbles_server_url":null,"bluebubbles_user_address":null,"bluebubbles_caller_context":null}`, "http://bridge", "owner@example.com", "saved persona", "saved-password"},
		{"clear persona", `{"bluebubbles_caller_context":""}`, "http://bridge", "owner@example.com", "", "saved-password"},
		{"clear address", `{"bluebubbles_user_address":""}`, "http://bridge", "", "saved persona", "saved-password"},
		{"clear url", `{"bluebubbles_server_url":""}`, "", "owner@example.com", "saved persona", "saved-password"},
		{"replace", `{"bluebubbles_server_url":"http://new","bluebubbles_user_address":"new@example.com","bluebubbles_caller_context":"new persona","bluebubbles_password":"new-password"}`, "http://new", "new@example.com", "new persona", "new-password"},
		{"empty password", `{"bluebubbles_password":""}`, "http://bridge", "owner@example.com", "saved persona", "saved-password"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := baseConfig()
			c.Channel = domain.ChannelIMessage
			c.BluebubblesServerURL = "http://bridge"
			c.BluebubblesUserAddress = "owner@example.com"
			c.BluebubblesCallerContext = "saved persona"
			c.BluebubblesPassword = "saved-password"
			var req domain.UpdateConfigRequest
			if err := json.Unmarshal([]byte(tc.payload), &req); err != nil {
				t.Fatal(err)
			}
			applyUpdate(c, req, "")
			if c.BluebubblesServerURL != tc.url || c.BluebubblesUserAddress != tc.address || c.BluebubblesCallerContext != tc.persona || c.BluebubblesPassword != tc.password {
				t.Fatal("partial JSON update did not preserve/clear the intended fields")
			}
		})
	}
}
