package server

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"
	"time"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type selectionResolveFunc func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection

func (f selectionResolveFunc) Resolve(ctx context.Context, text string, candidates []jev.Candidate, options jev.Options) jev.Selection {
	return f(ctx, text, candidates, options)
}

func selectionFixture(t *testing.T) (*harnessSelector, harness.Status, harness.Frame, config.JevIntentSettings) {
	t.Helper()
	s := newHarnessSelector()
	status := harness.Status{Connected: true, MachineID: "mac", ServerInstanceID: "daemon"}
	var listing harness.Frame
	if err := json.Unmarshal([]byte(`{"machineId":"mac","agents":[{"agentId":"airplane","recap":"Created airplane","workspace":{"path":"/airplane"}},{"agentId":"house","recap":"Created house with garden","workspace":{"path":"/house"}}]}`), &listing); err != nil {
		t.Fatal(err)
	}
	s.remember(listing, nil, status)
	return s, status, harness.Frame{"type": "turn.send", "machineId": "mac", "agentId": "airplane", "text": "Add trees to the house garden", "idempotencyKey": "unchanged"}, config.JevIntentSettings{Enabled: true, Endpoint: "https://proxy.test/jev/decisions", APIKey: "test", TimeoutMS: 100}
}

func TestHarnessSelectionUsesJevAgentBeforeDispatch(t *testing.T) {
	s, status, frame, settings := selectionFixture(t)
	original := harness.Frame{}
	for k, v := range frame {
		original[k] = v
	}
	s.resolver = selectionResolveFunc(func(_ context.Context, text string, candidates []jev.Candidate, _ jev.Options) jev.Selection {
		if text != frame["text"] || len(candidates) != 2 {
			t.Error("lost task evidence")
		}
		return jev.Selection{Intent: "session_1"}
	})
	result := s.selectAgent(context.Background(), frame, status, settings)
	if result.Mode != "jev" || result.AgentID != "house" || result.MachineID != "mac" {
		t.Fatalf("JEV not authoritative: %+v", result)
	}
	if !reflect.DeepEqual(frame, original) {
		t.Fatal("selector changed input instead of returning resolved target")
	}
}

func TestHarnessSelectionFallbackAndDisabled(t *testing.T) {
	for _, name := range []string{"disabled", "missing_config", "stale", "restart", "offline", "absent_target", "oversized_list", "oversized_metadata", "failed_list", "cancelled"} {
		t.Run(name, func(t *testing.T) {
			s, status, frame, settings := selectionFixture(t)
			s.resolver = selectionResolveFunc(func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection {
				t.Error("unexpected provider call")
				return jev.Selection{}
			})
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			switch name {
			case "disabled":
				settings.Enabled = false
			case "missing_config":
				settings.APIKey = ""
			case "stale":
				s.captured = time.Now().Add(-31 * time.Second)
			case "restart":
				status.ServerInstanceID = "new"
			case "offline":
				status.Connected = false
			case "absent_target":
				frame["agentId"] = "missing"
			case "oversized_list":
				s.remember(harness.Frame{"machineId": "mac", "agents": make([]any, 33)}, nil, status)
			case "oversized_metadata":
				s.remember(harness.Frame{"machineId": "mac", "agents": []any{map[string]any{"agentId": "airplane", "recap": string(make([]byte, 1001))}}}, nil, status)
			case "failed_list":
				s.remember(harness.Frame{"error": "offline"}, nil, status)
			case "cancelled":
				cancel()
			}
			result := s.selectAgent(ctx, frame, status, settings)
			want := "fallback"
			if name == "disabled" {
				want = "disabled"
			}
			if result.Mode != want || result.AgentID != frame["agentId"] || result.MachineID != "mac" {
				t.Fatalf("fallback lost original target: %+v", result)
			}
		})
	}
}

func TestHarnessSelectionTimeoutFallsBack(t *testing.T) {
	s, status, frame, settings := selectionFixture(t)
	settings.TimeoutMS = 10
	s.resolver = selectionResolveFunc(func(ctx context.Context, _ string, _ []jev.Candidate, _ jev.Options) jev.Selection {
		<-ctx.Done()
		return jev.Selection{Intent: "session_1"}
	})
	result := s.selectAgent(context.Background(), frame, status, settings)
	if result.Mode != "fallback" || result.AgentID != "airplane" || result.Reason != "cancelled_or_timeout" {
		t.Fatalf("late result overrode fallback: %+v", result)
	}
}

func TestHarnessSelectionUnknownOrNoneFallsBack(t *testing.T) {
	for _, choice := range []string{"", "none", "invented"} {
		s, status, frame, settings := selectionFixture(t)
		s.resolver = selectionResolveFunc(func(context.Context, string, []jev.Candidate, jev.Options) jev.Selection {
			return jev.Selection{Intent: choice}
		})
		result := s.selectAgent(context.Background(), frame, status, settings)
		if result.Mode != "fallback" || result.AgentID != "airplane" {
			t.Fatalf("uncertain result changed target: %+v", result)
		}
	}
}
