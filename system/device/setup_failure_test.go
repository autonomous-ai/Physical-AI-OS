package device

import (
	"errors"
	"fmt"
	"testing"

	"go.autonomous.ai/os/system/network"
)

func TestSetupFailureReason(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want string
	}{
		{name: "success", err: nil, want: ""},
		{name: "wifi join", err: fmt.Errorf("setup network: %w", &network.SetupError{Reason: network.FailureWrongPassword}), want: "wrong_password"},
		{name: "agent install", err: &stageError{reason: failureAgentSetup, err: errors.New("no llm models found")}, want: "agent_setup_failed"},
		{name: "agent never ready", err: &stageError{reason: failureAgentTimeout, err: errors.New("timeout")}, want: "agent_timeout"},
		{name: "anything else", err: errors.New("hash admin password: boom"), want: "unknown"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := SetupFailureReason(tc.err); got != tc.want {
				t.Errorf("SetupFailureReason() = %q, want %q", got, tc.want)
			}
		})
	}
}
