package device

import (
	"errors"

	"go.autonomous.ai/os/system/network"
)

// Setup failure reasons for the stages after the WiFi join. The join itself is
// classified by network.SetupFailureReason; both share the stable
// `setup_failure_reason` log field so every failed setup can be grouped by
// cause in Graylog.
const (
	// FailureSetupRuntime leaves the working LAN available for retry/diagnostics.
	FailureSetupRuntime = "runtime_startup_failed"
	failureAgentSetup   = "agent_setup_failed" // the agent runtime could not be installed/configured
	failureAgentTimeout = "agent_timeout"      // the agent gateway never became ready
)

// stageError tags an error with the setup stage that failed.
type stageError struct {
	reason string
	err    error
}

func (e *stageError) Error() string { return e.err.Error() }
func (e *stageError) Unwrap() error { return e.err }

// SetupFailureReason returns the `setup_failure_reason` value for an error
// returned by Setup, or "" for nil.
func SetupFailureReason(err error) string {
	if err == nil {
		return ""
	}
	var se *stageError
	if errors.As(err, &se) {
		return se.reason
	}
	if r := network.FailureReasonOf(err); r != "" {
		return string(r)
	}
	return string(network.FailureUnknown)
}
