// Package device owns the device-level service: setup, channels, config updates,
// agent-runtime switching, status reporting, ROBOT.md parsing and timezone.
package device

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"sync"
	"time"

	"go.autonomous.ai/os/system/beclient"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// ErrAgentRuntimeSwitchInProgress prevents concurrent switch-runtime invocations.
// The switcher owns systemd units and must run exclusively.
var ErrAgentRuntimeSwitchInProgress = errors.New("agent runtime switch already in progress")

type Service struct {
	config          *config.Config
	networkService  *network.Service
	agentGateway    domain.AgentGateway
	beClient        *beclient.Client
	statusLED       *statusled.Service
	setupState      setupState
	setupRuntime    setupRuntime
	runtimeSwitchMu sync.Mutex
	wakeApply       wakeWordApply
	// Optional command override for isolated service tests.
	halRestartCommand func(context.Context) error
	halInputModeApply func(context.Context, string, bool) error
}

func ProvideService(config *config.Config, ns *network.Service, gw domain.AgentGateway, be *beclient.Client, sled *statusled.Service) *Service {
	SeedAgentRuntimeFromGateway(config)
	return &Service{
		config:         config,
		networkService: ns,
		agentGateway:   gw,
		beClient:       be,
		statusLED:      sled,
		setupState:     setupState{phase: SetupPhaseIdle},
	}
}

// restartHAL restarts HAL in the background so it re-reads config.json, then
// refreshes the boot-time config baseline to avoid a redundant restart.
func (s *Service) restartHAL(reason string) {
	go func() {
		if err := s.restartHALAndWait(context.Background(), reason); err != nil {
			slog.Warn("hal restart failed", "component", "device", "reason", reason, "error", err)
		}
	}()
}

// restartHALAndWait reports the command outcome, not voice-pipeline readiness.
// Non-wake callers retain the asynchronous restartHAL wrapper.
func (s *Service) restartHALAndWait(ctx context.Context, reason string) error {
	slog.Info("restarting hal", "component", "device", "reason", reason)
	if s.halRestartCommand != nil {
		if err := s.halRestartCommand(ctx); err != nil {
			return fmt.Errorf("restart HAL: %w", err)
		}
	} else {
		cmd := exec.CommandContext(ctx, "systemctl", "restart", "hal")
		cmd.WaitDelay = time.Second
		if out, err := cmd.CombinedOutput(); err != nil {
			if ctx.Err() != nil {
				return fmt.Errorf("restart HAL: %w", ctx.Err())
			}
			return fmt.Errorf("restart HAL: %w (%s)", err, out)
		}
	}
	slog.Info("hal restarted", "component", "device", "reason", reason)
	if err := config.SnapshotHALConfig(); err != nil {
		slog.Warn("hal config snapshot failed", "component", "device", "error", err)
	}
	return nil
}

// applyTTSConfig pushes a voice change into the running HAL, falling back to a
// restart on failure.
func (s *Service) applyTTSConfig(c *config.Config) {
	go func() {
		// Send the resolved key: HAL treats an empty key as "keep current", which
		// would leave a cleared vendor key live.
		if err := hal.ApplyTTSConfig(c.TTSProvider, c.TTSVoice, c.GetTTSAPIKey(), c.TTSBaseURL, c.GetTTSSpeed()); err != nil {
			slog.Warn("hal tts config apply failed, restarting instead",
				"component", "device", "error", err)
			s.restartHAL("voice config change")
			return
		}
		slog.Info("hal tts config applied live", "component", "device",
			"provider", c.TTSProvider, "voice", c.TTSVoice)
		// Keep the boot-time baseline in step to avoid a needless restart.
		if err := config.SnapshotHALConfig(); err != nil {
			slog.Warn("hal config snapshot failed", "component", "device", "error", err)
		}
	}()
}

// WaitForAgentReady polls agentGateway.IsReady until it returns true or the timeout elapses.
func (s *Service) WaitForAgentReady(timeout time.Duration) bool {
	return waitForAgentReady(s.agentGateway, timeout, 0, 500*time.Millisecond)
}

// WaitForAgentReadyStable requires IsReady to stay true for stableFor, so a
// greeting cannot race a startup reconciliation restart.
func (s *Service) WaitForAgentReadyStable(timeout, stableFor time.Duration) bool {
	return waitForAgentReady(s.agentGateway, timeout, stableFor, 500*time.Millisecond)
}

// AgentReady reports whether the active gateway is answering now (single probe).
func (s *Service) AgentReady() bool {
	return s.agentGateway != nil && s.agentGateway.IsReady()
}

type agentReadiness interface {
	IsReady() bool
}

func waitForAgentReady(gateway agentReadiness, timeout, stableFor, pollInterval time.Duration) bool {
	if gateway == nil {
		return false
	}
	deadline := time.Now().Add(timeout)
	var readySince time.Time
	for {
		now := time.Now()
		if gateway.IsReady() {
			if readySince.IsZero() {
				readySince = now
			}
			if now.Sub(readySince) >= stableFor {
				return true
			}
		} else {
			readySince = time.Time{}
		}
		if !now.Before(deadline) {
			return false
		}
		time.Sleep(pollInterval)
	}
}
