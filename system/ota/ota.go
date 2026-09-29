// Package ota is os-server's client for the bootstrap worker's OTA endpoints,
// shared by the web UI and the cloud (one allowlist and per-target rate limiter).
package ota

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// AgentTarget is the virtual target for the configured runtime's agent CLI.
const AgentTarget = "agent"

// MinTriggerInterval is the per-target rate limit of TriggerUpdate.
const MinTriggerInterval = 30 * time.Second

// bootstrapBaseURL is the bootstrap worker's local API; a var for tests.
var bootstrapBaseURL = "http://127.0.0.1:8080"

// allowedTargets are the components an operator may force-update.
var allowedTargets = map[string]bool{
	domain.OTAKeyOSServer: true, domain.OTAKeyBootstrap: true, domain.OTAKeyWeb: true, domain.OTAKeyHal: true,
	domain.OTAKeyDevice: true,
	domain.OTAKeyCodex:  true, domain.OTAKeyClaudeCode: true, domain.OTAKeyOpenCode: true, domain.OTAKeyPicoClaw: true,
	domain.OTAKeyHermes: true,
}

// lastFire tracks the last trigger time per resolved target.
var (
	lastFire   = map[string]time.Time{}
	lastFireMu sync.Mutex
)

var (
	// ErrUnknownTarget: target not in the allowlist (HTTP 400).
	ErrUnknownTarget = errors.New("unknown target")
	// ErrBuildRequest: request could not be built (HTTP 500).
	ErrBuildRequest = errors.New("build request")
	// ErrBootstrapUnreachable: bootstrap did not answer (HTTP 502).
	ErrBootstrapUnreachable = errors.New("bootstrap unreachable")
)

// RateLimitedError: same target fired within MinTriggerInterval (HTTP 429).
type RateLimitedError struct {
	Target     string
	RetryAfter time.Duration
}

// RetryAfterSeconds rounds RetryAfter up to whole seconds (never 0).
func (e *RateLimitedError) RetryAfterSeconds() int { return int(e.RetryAfter.Seconds()) + 1 }

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("software-update %s rate-limited, retry in %ds", e.Target, e.RetryAfterSeconds())
}

// BootstrapRefusedError: bootstrap answered /force-update with non-200 (HTTP 502).
type BootstrapRefusedError struct {
	Target string
	Status string
	Body   string
}

func (e *BootstrapRefusedError) Error() string {
	return fmt.Sprintf("bootstrap refused %s: %s %s", e.Target, e.Status, e.Body)
}

// ComponentVersion mirrors bootstrap.ComponentVersion (not imported, to avoid the dependency).
type ComponentVersion struct {
	Current         string `json:"current"`
	Target          string `json:"target"`
	MinVersion      string `json:"min_version"`
	UpdateAvailable bool   `json:"update_available"`
	HeldByFloor     bool   `json:"held_by_floor"`
}

// ResolveTarget maps "agent" to the configured runtime's OTA key.
func ResolveTarget(cfg *config.Config, target string) string {
	if target == AgentTarget {
		return device.CurrentAgentRuntimeFromConfig(cfg)
	}
	return target
}

// TriggerUpdate force-installs the published version of target via bootstrap.
// target: os-server | bootstrap | web | hal | device | <agent CLI> | agent.
// Returns the resolved target; nil error means "started", not "done".
func TriggerUpdate(ctx context.Context, cfg *config.Config, target string) (string, error) {
	resolved := ResolveTarget(cfg, target)
	if !allowedTargets[resolved] {
		return resolved, fmt.Errorf("%w: %s", ErrUnknownTarget, resolved)
	}

	lastFireMu.Lock()
	if last, ok := lastFire[resolved]; ok {
		if wait := MinTriggerInterval - time.Since(last); wait > 0 {
			lastFireMu.Unlock()
			return resolved, &RateLimitedError{Target: resolved, RetryAfter: wait}
		}
	}
	lastFire[resolved] = time.Now()
	lastFireMu.Unlock()

	// force-update, not force-check: force-check respects min_version and could silently no-op.
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, bootstrapBaseURL+"/force-update/"+resolved, nil)
	if err != nil {
		return resolved, fmt.Errorf("%w: %v", ErrBuildRequest, err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return resolved, fmt.Errorf("%w: %v", ErrBootstrapUnreachable, err)
	}
	defer resp.Body.Close()
	// Bootstrap keeps its own allowlist; propagate a refusal.
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		return resolved, &BootstrapRefusedError{Target: resolved, Status: resp.Status, Body: strings.TrimSpace(string(body))}
	}
	return resolved, nil
}

// Versions proxies bootstrap's /versions and adds an "agent" alias.
func Versions(ctx context.Context, cfg *config.Config) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, bootstrapBaseURL+"/versions", nil)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBuildRequest, err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBootstrapUnreachable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("bootstrap versions: " + resp.Status)
	}
	var versions map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&versions); err != nil {
		return nil, fmt.Errorf("decode versions: %w", err)
	}
	if versions == nil {
		versions = map[string]any{}
	}
	if entry, ok := versions[device.CurrentAgentRuntimeFromConfig(cfg)]; ok {
		versions[AgentTarget] = entry
	}
	return versions, nil
}

// Component extracts one typed entry from a Versions result.
func Component(versions map[string]any, key string) (ComponentVersion, bool) {
	entry, ok := versions[key]
	if !ok {
		return ComponentVersion{}, false
	}
	raw, err := json.Marshal(entry)
	if err != nil {
		return ComponentVersion{}, false
	}
	var cv ComponentVersion
	if err := json.Unmarshal(raw, &cv); err != nil {
		return ComponentVersion{}, false
	}
	return cv, true
}

// Updating lists the components bootstrap is installing right now.
func Updating(ctx context.Context, cfg *config.Config) ([]string, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, bootstrapBaseURL+"/updating", nil)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBuildRequest, err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBootstrapUnreachable, err)
	}
	defer resp.Body.Close()
	var body struct {
		Updating []string `json:"updating"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&body); err != nil {
		return nil, fmt.Errorf("decode updating: %w", err)
	}
	runtime := device.CurrentAgentRuntimeFromConfig(cfg)
	out := append([]string{}, body.Updating...)
	for _, k := range body.Updating {
		if k == runtime {
			out = append(out, AgentTarget)
			break
		}
	}
	return out, nil
}

// WaitOptions tunes WaitUntilDone. Zero values take the defaults.
type WaitOptions struct {
	// Poll is the interval between Updating reads (default 3s).
	Poll time.Duration
	// AppearGrace: a target unseen this long after trigger counts as done (default 15s).
	AppearGrace time.Duration
}

// WaitUntilDone blocks until target leaves Updating or ctx ends.
// Transient errors are tolerated (bootstrap restarts when it is the target).
func WaitUntilDone(ctx context.Context, cfg *config.Config, target string, opts WaitOptions) error {
	if opts.Poll <= 0 {
		opts.Poll = 3 * time.Second
	}
	if opts.AppearGrace <= 0 {
		opts.AppearGrace = 15 * time.Second
	}
	start := time.Now()
	seen := false
	ticker := time.NewTicker(opts.Poll)
	defer ticker.Stop()
	for {
		if list, err := Updating(ctx, cfg); err == nil {
			in := false
			for _, k := range list {
				if k == target {
					in = true
					break
				}
			}
			switch {
			case in:
				seen = true
			case seen, time.Since(start) >= opts.AppearGrace:
				return nil
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
