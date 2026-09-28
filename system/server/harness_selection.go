package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type harnessSelectorResolver interface {
	Resolve(context.Context, string, []jev.Candidate, jev.Options) jev.Selection
}

type harnessSelector struct {
	mu                sync.Mutex
	candidates        []jev.Candidate
	agentIDs          []string
	machine, instance string
	captured          time.Time
	resolver          harnessSelectorResolver
	// Tests can observe outcomes without recording private provider payloads.
	report func(string, string, string, int64)
}

func newHarnessSelector() *harnessSelector {
	return &harnessSelector{resolver: jev.NewHarnessResolver(), report: func(outcome, actual, proposed string, elapsed int64) {
		slog.Info("Harness Jev selection", "component", "harness", "outcome", outcome,
			"main_agent_id", actual, "selected_agent_id", proposed, "decision_ms", elapsed)
	}}
}

// Only reuse discovery already requested by the skill. Never truncate the agent
// set: excluding a candidate would make comparison misleading.
func (s *harnessSelector) remember(result harness.Frame, err error, status harness.Status) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.candidates, s.agentIDs = nil, nil
	if err != nil || result["error"] != nil || !status.Connected || status.MachineID == "" || status.ServerInstanceID == "" || result["machineId"] != status.MachineID {
		return
	}
	agents, ok := result["agents"].([]any)
	if !ok || len(agents) == 0 || len(agents) > 32 {
		return
	}
	candidates := make([]jev.Candidate, 0, len(agents))
	ids := make([]string, 0, len(agents))
	seen := map[string]bool{}
	for i, raw := range agents {
		agent, ok := raw.(map[string]any)
		if !ok {
			return
		}
		id, _ := agent["agentId"].(string)
		if id == "" || len(id) > 128 || seen[id] {
			return
		}
		seen[id] = true
		facts := map[string]any{}
		for _, key := range []string{"name", "recap", "workspace", "packageId", "runtime", "state", "engine"} {
			if value, exists := agent[key]; exists {
				facts[key] = value
			}
		}
		description, err := json.Marshal(facts)
		if err != nil || len(description) > 1000 {
			return
		}
		candidates = append(candidates, jev.Candidate{ID: fmt.Sprintf("session_%d", i), Description: string(description)})
		ids = append(ids, id)
	}
	s.candidates, s.agentIDs = candidates, ids
	s.machine, s.instance, s.captured = status.MachineID, status.ServerInstanceID, time.Now()
}

// harnessSelection is local OS metadata, never a Harness wire frame.
type harnessSelection struct {
	Mode      string `json:"mode"`
	AgentID   string `json:"agentId"`
	MachineID string `json:"machineId"`
	Reason    string `json:"reason"`
}

// selectAgent runs before the helper reserves a delivery. Every abstention or
// infrastructure failure returns the original target, just like intent fallback.
func (s *harnessSelector) selectAgent(ctx context.Context, frame harness.Frame, status harness.Status, settings config.JevIntentSettings) harnessSelection {
	proposed, _ := frame["agentId"].(string)
	machine, _ := frame["machineId"].(string)
	result := harnessSelection{Mode: "fallback", AgentID: proposed, MachineID: machine}
	started := time.Now()
	if s != nil && s.report != nil {
		defer func() {
			s.report(result.Mode+"_"+result.Reason, proposed, result.AgentID, time.Since(started).Milliseconds())
		}()
	}
	if !settings.Enabled {
		result.Mode, result.Reason = "disabled", "disabled"
		return result
	}
	if s == nil || s.resolver == nil {
		result.Reason = "unavailable"
		return result
	}
	if settings.Endpoint == "" || settings.APIKey == "" {
		result.Reason = "missing_config"
		return result
	}
	text, _ := frame["text"].(string)
	if strings.TrimSpace(text) == "" || len(text) > 2000 || proposed == "" {
		result.Reason = "input"
		return result
	}
	s.mu.Lock()
	valid := status.Connected && machine == status.MachineID && s.machine == status.MachineID && s.instance == status.ServerInstanceID && time.Since(s.captured) <= 30*time.Second
	candidates := append([]jev.Candidate(nil), s.candidates...)
	ids := append([]string(nil), s.agentIDs...)
	s.mu.Unlock()
	found := false
	for _, id := range ids {
		found = found || id == proposed
	}
	if !valid || !found {
		result.Reason = "snapshot"
		return result
	}
	if ctx.Err() != nil {
		result.Reason = "cancelled"
		return result
	}
	budget := time.Duration(settings.TimeoutMS) * time.Millisecond
	if budget <= 0 {
		budget = 1500 * time.Millisecond
	} else if budget > 3*time.Second {
		budget = 3 * time.Second
	}
	worker, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	choice := s.resolver.Resolve(worker, text, candidates, jev.Options{Enabled: true, Endpoint: settings.Endpoint, APIKey: settings.APIKey, Timeout: budget})
	if worker.Err() != nil {
		result.Reason = "cancelled_or_timeout"
		return result
	}
	for i, candidate := range candidates {
		if choice.Intent == candidate.ID {
			result.Mode, result.AgentID, result.Reason = "jev", ids[i], "selected"
			return result
		}
	}
	result.Reason = "uncertain_or_unavailable"
	return result
}
