// Package ota is os-server's client for the local bootstrap worker's OTA
// endpoints (GET /versions, GET /updating, POST /force-update/<target>).
//
// It is the single implementation behind both callers: the web UI's Versions
// card (HTTP handlers in system/server) and the cloud (MQTT kinds
// system.ota_versions / system.software_update). Both therefore share the
// target allowlist, the "agent" alias and — importantly — one per-target rate
// limiter, so the web and the cloud cannot together fire back-to-back updates.
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

// AgentTarget is the virtual target/key standing for "the configured runtime's
// agent CLI". Callers (the Versions card, the cloud) know there is an agent CLI
// but not WHICH one, so it is resolved here rather than shipped to them. It maps
// to the OTA key of the configured runtime — they share the same names by
// construction (domain.AgentRuntime* == domain.OTAKey* for the CLIs).
const AgentTarget = "agent"

// MinTriggerInterval is the per-target rate limit of TriggerUpdate. Bootstrap's
// downloader is idempotent but the resulting service restarts (os-server +
// systemd reload + journal noise) are not free; 30 s is enough to absorb a
// double-click without hiding genuine retries.
const MinTriggerInterval = 30 * time.Second

// bootstrapBaseURL is the bootstrap worker's local HTTP API. A variable only so
// tests can point it at an httptest server.
var bootstrapBaseURL = "http://127.0.0.1:8080"

// allowedTargets are the components an operator may force-update. Bootstrap
// keeps its own allowlist (forceTargetAllowed); a disagreement surfaces as a
// *BootstrapRefusedError rather than a silent success.
var allowedTargets = map[string]bool{
	domain.OTAKeyOSServer: true, domain.OTAKeyBootstrap: true, domain.OTAKeyWeb: true, domain.OTAKeyHal: true,
	domain.OTAKeyDevice: true,
	domain.OTAKeyCodex:  true, domain.OTAKeyClaudeCode: true, domain.OTAKeyOpenCode: true, domain.OTAKeyPicoClaw: true,
	domain.OTAKeyHermes: true,
}

// lastFire tracks the last time each (resolved) OTA target was triggered, so a
// stuck/looping caller can't kick off back-to-back force-updates.
var (
	lastFire   = map[string]time.Time{}
	lastFireMu sync.Mutex
)

var (
	// ErrUnknownTarget: the target is not in the allowlist (HTTP 400).
	ErrUnknownTarget = errors.New("unknown target")
	// ErrBuildRequest: the request to bootstrap could not be built (HTTP 500).
	ErrBuildRequest = errors.New("build request")
	// ErrBootstrapUnreachable: bootstrap did not answer at all (HTTP 502).
	ErrBootstrapUnreachable = errors.New("bootstrap unreachable")
)

// RateLimitedError is returned by TriggerUpdate when the same target was fired
// less than MinTriggerInterval ago (HTTP 429).
type RateLimitedError struct {
	Target     string
	RetryAfter time.Duration
}

// RetryAfterSeconds rounds RetryAfter up to whole seconds (never 0), the value
// the Retry-After header and the error message carry.
func (e *RateLimitedError) RetryAfterSeconds() int { return int(e.RetryAfter.Seconds()) + 1 }

func (e *RateLimitedError) Error() string {
	return fmt.Sprintf("software-update %s rate-limited, retry in %ds", e.Target, e.RetryAfterSeconds())
}

// BootstrapRefusedError is returned when bootstrap answered /force-update with a
// non-200 status (HTTP 502).
type BootstrapRefusedError struct {
	Target string
	Status string
	Body   string
}

func (e *BootstrapRefusedError) Error() string {
	return fmt.Sprintf("bootstrap refused %s: %s %s", e.Target, e.Status, e.Body)
}

// ComponentVersion is one entry of Versions — mirrors bootstrap.ComponentVersion
// (not imported to keep the bootstrap worker out of os-server's dependencies).
type ComponentVersion struct {
	Current         string `json:"current"`
	Target          string `json:"target"`
	MinVersion      string `json:"min_version"`
	UpdateAvailable bool   `json:"update_available"`
	HeldByFloor     bool   `json:"held_by_floor"`
}

// ResolveTarget maps the virtual "agent" target to the configured runtime's OTA
// key; any other target is returned unchanged.
func ResolveTarget(cfg *config.Config, target string) string {
	if target == AgentTarget {
		// Hermes included: bootstrap applies it once the metadata entry is
		// commit-pinned (domain.OTAKeyHermes); an unpinned entry is simply not
		// reported by /versions, so the button never appears for it.
		return device.CurrentAgentRuntimeFromConfig(cfg)
	}
	return target
}

// TriggerUpdate installs the published version of one component now, via the
// bootstrap worker — the equivalent of `software-update <target>` over SSH. The
// staged-rollout floor (min_version) governs the AUTOMATIC worker only; an
// operator updating one device on purpose is not subject to it.
//
// target: os-server | bootstrap | web | hal | device | <agent CLI> | agent.
// The resolved target (e.g. "hermes" for "agent") is returned even on error.
// Bootstrap installs asynchronously: a nil error means "started", not "done" —
// poll Updating / Versions for the outcome.
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

	// force-update, NOT force-check: this stands for "run `software-update
	// <target>` on this device", which installs the published version outright.
	// force-check would re-run the AUTOMATIC decision instead, and that one
	// respects min_version — so a component whose rollout floor has not been
	// promoted would silently do nothing while the caller was told OK.
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, bootstrapBaseURL+"/force-update/"+resolved, nil)
	if err != nil {
		return resolved, fmt.Errorf("%w: %v", ErrBuildRequest, err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return resolved, fmt.Errorf("%w: %v", ErrBootstrapUnreachable, err)
	}
	defer resp.Body.Close()
	// Propagate a refusal instead of reporting success: bootstrap keeps its own
	// target allowlist, so the two can disagree (they did while the agent CLIs
	// were being added).
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
		return resolved, &BootstrapRefusedError{Target: resolved, Status: resp.Status, Body: strings.TrimSpace(string(body))}
	}
	return resolved, nil
}

// Versions reports, per component, what this device runs vs what the OTA feed
// offers (entries shaped like ComponentVersion), proxied from bootstrap — it
// owns metadata + version detection. One alias is added: "agent" duplicates the
// entry of the configured runtime's CLI, so callers never need to know which
// runtime this device runs.
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
	// The Agent row aliases the configured runtime's entry. Hermes appears here
	// only when bootstrap reports it, i.e. the metadata entry is commit-pinned
	// and the on-device updater can apply the pin (see domain.OTAKeyHermes).
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

// Updating lists the components the bootstrap worker is installing right now.
// Cheap by design (no metadata fetch) — the UI polls it every couple of seconds.
// Mirrors the "agent" alias of Versions.
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
	// Poll is the interval between Updating reads (default 3 s).
	Poll time.Duration
	// AppearGrace: if the target has not shown up in Updating within this long
	// after the trigger, the install is assumed to have finished (or never
	// started) before the first poll saw it (default 15 s).
	AppearGrace time.Duration
}

// WaitUntilDone blocks until bootstrap no longer lists target as updating, or
// ctx ends (returned as ctx.Err()). Transient Updating errors are tolerated —
// bootstrap itself restarts when it is the target. A target never seen within
// AppearGrace (with bootstrap answering) counts as done.
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
