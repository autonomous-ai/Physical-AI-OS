package domain

import (
	"testing"

	"github.com/go-playground/validator/v10"
)

// baseSetupRequest returns a request with every required field populated.
func baseSetupRequest() SetupRequest {
	return SetupRequest{
		SSID:             "home-wifi",
		Password:         "hunter2",
		LLMBaseURL:       "https://api.example.com",
		LLMAPIKey:        "sk-test",
		DeviceID:         "lamp-7f72",
		Channel:          "telegram",
		TelegramBotToken: "123:abc",
		TelegramUserID:   "456",
	}
}

// TestSetupRequestNetworkFieldsOptional checks an absent SSID/password passes validation (wired setup).
func TestSetupRequestNetworkFieldsOptional(t *testing.T) {
	v := validator.New()

	tests := []struct {
		name     string
		mutate   func(*SetupRequest)
		wantPass bool
	}{
		{
			name:     "full wifi request",
			mutate:   func(*SetupRequest) {},
			wantPass: true,
		},
		{
			name:     "no ssid and no password",
			mutate:   func(r *SetupRequest) { r.SSID = ""; r.Password = "" },
			wantPass: true,
		},
		{
			name:     "ssid without password",
			mutate:   func(r *SetupRequest) { r.Password = "" },
			wantPass: true,
		},
		{
			name:     "missing llm api key still rejected",
			mutate:   func(r *SetupRequest) { r.LLMAPIKey = "" },
			wantPass: false,
		},
		{
			name:     "missing device id still rejected",
			mutate:   func(r *SetupRequest) { r.DeviceID = "" },
			wantPass: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := baseSetupRequest()
			tt.mutate(&req)
			err := v.Struct(req)
			if tt.wantPass && err != nil {
				t.Fatalf("validation rejected a request that should pass: %v", err)
			}
			if !tt.wantPass && err == nil {
				t.Fatal("validation accepted a request that should be rejected")
			}
		})
	}
}
