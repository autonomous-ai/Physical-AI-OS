package intent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"reflect"
	"slices"
	"strings"
	"sync/atomic"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent/jev"
)

type fakeJevDecider func(context.Context, string, string, []jev.Candidate) (string, error)

func (f fakeJevDecider) Resolve(ctx context.Context, text string, candidates []jev.Candidate, options jev.Options) jev.Selection {
	id, _ := f(ctx, options.APIKey, text, candidates)
	return jev.Selection{Intent: id}
}

type fakeJevSelectionResolver func([]jev.Candidate) jev.Selection

func (f fakeJevSelectionResolver) Resolve(_ context.Context, _ string, candidates []jev.Candidate, _ jev.Options) jev.Selection {
	return f(candidates)
}

func TestJevCatalogCoversCommandRulesAndParameterDomains(t *testing.T) {
	seen := map[string]bool{}
	for _, candidate := range jev.Candidates() {
		if seen[candidate.ID] {
			t.Fatalf("duplicate candidate %q", candidate.ID)
		}
		seen[candidate.ID] = true
		if !slices.ContainsFunc(rules, func(r rule) bool { return r.name == candidate.ID }) {
			t.Fatalf("candidate %q has no executable rule", candidate.ID)
		}
		var expected []string
		parameter := ""
		switch candidate.ID {
		case "led_color":
			parameter = "color"
			for _, option := range colorKeywords {
				expected = append(expected, option.keywords[0])
			}
		case "servo_track":
			parameter = "target"
			for _, option := range trackTargets {
				if !slices.Contains(expected, option.label) {
					expected = append(expected, option.label)
				}
			}
		}
		if parameter == "" {
			if len(candidate.Parameters) != 0 {
				t.Fatalf("fixed rule %q cannot take parameters", candidate.ID)
			}
			continue
		}
		actual := slices.Clone(candidate.Parameters[parameter].Options)
		slices.Sort(actual)
		slices.Sort(expected)
		if len(candidate.Parameters) != 1 || !slices.Equal(actual, expected) {
			t.Fatalf("%s domain differs from local rules: got %v, want %v", candidate.ID, actual, expected)
		}
		for _, value := range actual {
			text, ok := semanticExecutionText(jev.Selection{Intent: candidate.ID, Parameters: map[string]string{parameter: value}})
			if !ok {
				t.Fatalf("canonical parameter rejected: %s=%s", parameter, value)
			}
			if parameter == "target" && extractTrackTarget(text) != value {
				t.Fatalf("target %q changed during execution translation: %q", value, text)
			}
			if parameter == "color" {
				_, name, found := extractColor(text)
				if !found || strings.ToLower(name) != value {
					t.Fatalf("color %q changed during execution translation: %q", value, text)
				}
			}
		}
	}
	for _, r := range rules {
		if !seen[r.name] {
			t.Errorf("local rule %q is missing from Jev", r.name)
		}
	}
}

func TestJevExpandedActionsUseExistingHALContracts(t *testing.T) {
	for _, tc := range []struct {
		intent string
		params map[string]string
		want   []string
	}{
		{"led_color", map[string]string{"color": "blue"}, []string{"/led/effect/stop ", `/led/solid {"color":[0,100,255]}`}},
		{"scene_focus", nil, []string{`/scene {"scene":"focus"}`}},
		{"scene_reading", nil, []string{`/scene {"scene":"reading"}`}},
		{"scene_relax", nil, []string{`/scene {"scene":"relax"}`}},
		{"scene_movie", nil, []string{`/scene {"scene":"movie"}`}},
		{"scene_energize", nil, []string{`/scene {"scene":"energize"}`}},
		{"scene_night", nil, []string{`/scene {"scene":"night"}`, `/emotion {"emotion":"sleepy","intensity":0.4}`}},
		{"scene_off", nil, []string{"/scene/off "}},
		{"mute_speaker", nil, []string{"/speaker/mute "}},
		{"unmute_speaker", nil, []string{"/speaker/unmute "}},
		{"music_stop", nil, []string{"/audio/stop "}},
		{"stop_talking", nil, []string{"/tts/stop "}},
		{"servo_track_stop", nil, []string{"/servo/track/stop "}},
		{"servo_track", map[string]string{"target": "cell phone"}, []string{`/servo/track {"target":["cell phone"]}`}},
		{"what_time", nil, nil},
	} {
		t.Run(tc.intent, func(t *testing.T) {
			var calls []string
			routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/emotion/status" {
					_, _ = w.Write([]byte(`{"sleeping":false}`))
					return
				}
				body, _ := io.ReadAll(r.Body)
				calls = append(calls, r.URL.Path+" "+string(body))
			})
			Configure(map[string]bool{device.CapLight: true, device.CapAudio: true, device.CapMedia: true, device.CapMotion: true, device.CapExpression: true})
			resolver := fakeJevSelectionResolver(func([]jev.Candidate) jev.Selection {
				if len(calls) != 0 {
					t.Fatal("hardware executed before classification")
				}
				return jev.Selection{Intent: tc.intent, Parameters: tc.params}
			})
			result := MatchWithFallback(context.Background(), "semantic test request", resolver,
				jev.Options{Enabled: true, Endpoint: "https://proxy.example.test/jev/decisions", APIKey: "test"})
			if result == nil || result.Rule != tc.intent || result.Source != "jev" || result.ExecutionFailed || !reflect.DeepEqual(calls, tc.want) {
				t.Fatalf("result=%+v calls=%v want=%v", result, calls, tc.want)
			}
		})
	}
}

func TestJevRejectsInvalidParametersBeforeHAL(t *testing.T) {
	calls := configureJevTest(t)
	Configure(map[string]bool{device.CapLight: true, device.CapMotion: true})
	for _, selection := range []jev.Selection{
		{Intent: "led_color"},
		{Intent: "led_color", Parameters: map[string]string{"color": "ultraviolet"}},
		{Intent: "led_color", Parameters: map[string]string{"color": "blue", "brightness": "200"}},
		{Intent: "led_color", Parameters: map[string]string{"target": "blue"}},
		{Intent: "led_off", Parameters: map[string]string{"color": "blue"}},
		{Intent: "servo_track"},
		{Intent: "servo_track", Parameters: map[string]string{"target": "cup\"]},\"speed\":999"}},
		{Intent: "servo_track", Parameters: map[string]string{"target": "unlisted object"}},
	} {
		if command := semanticCommand(selection, semanticCandidates()); command != nil {
			t.Errorf("invalid selection accepted: %+v", selection)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("parameter validation executed hardware")
	}
}

func TestJevParameterizedSelectionsRecheckCapabilities(t *testing.T) {
	calls := configureJevTest(t)
	Configure(map[string]bool{device.CapLight: true, device.CapMotion: true})
	offered := semanticCandidates()
	Configure(nil)
	for _, selection := range []jev.Selection{
		{Intent: "led_color", Parameters: map[string]string{"color": "blue"}},
		{Intent: "servo_track", Parameters: map[string]string{"target": "cup"}},
	} {
		if command := semanticCommand(selection, offered); command != nil {
			t.Fatalf("removed capability accepted: %+v", selection)
		}
	}
	if command := semanticCommand(jev.Selection{Intent: "what_time"}, offered); command == nil {
		t.Fatal("hardware-free time query should remain available")
	}
	if calls.Load() != 0 {
		t.Fatal("capability validation executed hardware")
	}
}

func configureJevTest(t *testing.T) *atomic.Int32 {
	t.Helper()
	calls := &atomic.Int32{}
	color := [3]int{160, 120, 80}
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/emotion/status" {
			_, _ = w.Write([]byte(`{"sleeping":false}`))
			return
		}
		calls.Add(1)
		if r.URL.Path == "/led/color" {
			_ = json.NewEncoder(w).Encode(map[string]any{"color": color})
		} else if r.URL.Path == "/led/solid" {
			var body struct {
				Color [3]int `json:"color"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			color = body.Color
		}
	})
	Configure(map[string]bool{device.CapLight: true, device.CapAudio: true})
	return calls
}

func TestJevLocalDisabledAndMissingKeyNeverCallProvider(t *testing.T) {
	configureJevTest(t)
	r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) {
		t.Fatal("fast path contacted provider")
		return "", nil
	})
	for _, tc := range []struct {
		text string
		opts jev.Options
		want string
	}{
		{"light off", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}, "led_off"},
		{"This lamp is too bright.", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", APIKey: "test"}, ""},
		{"This lamp is too bright.", jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true}, ""},
	} {
		got := MatchWithFallback(context.Background(), tc.text, r, tc.opts)
		if tc.want == "" && got != nil || tc.want != "" && (got == nil || got.Rule != tc.want || got.Source != "") {
			t.Fatalf("Match(%q) = %+v", tc.text, got)
		}
	}
}

func TestJevRecognitionHasNoEffectsAndExecutionUsesExistingRule(t *testing.T) {
	calls := configureJevTest(t)
	command := recognizeCommand("turn off the light")
	if command == nil || command.rule.name != "led_off" || calls.Load() != 0 {
		t.Fatal("recognition executed hardware or lost rule")
	}
	r := fakeJevDecider(func(ctx context.Context, key, text string, candidates []jev.Candidate) (string, error) {
		if calls.Load() != 0 || text != "This lamp is too bright." || key != "test" {
			t.Fatal("unexpected inference input or early side effect")
		}
		var ids []string
		for _, c := range candidates {
			ids = append(ids, c.ID)
		}
		if !slices.Contains(ids, "dim") || !slices.Contains(ids, "led_color") || !slices.Contains(ids, "what_time") || slices.Contains(ids, "servo_track") || slices.Contains(ids, "mute_speaker") {
			t.Fatalf("unexpected catalogue: %v", ids)
		}
		return "dim", nil
	})
	got := MatchWithFallback(context.Background(), "This lamp is too bright.", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"})
	if got == nil || got.Rule != "dim" || got.Source != "jev" || calls.Load() != 3 || !got.LEDChanged {
		t.Fatalf("semantic result = %+v, HAL calls=%d", got, calls.Load())
	}
	if !reflect.DeepEqual(got.Actions, []string{"GET /led/color", `POST /led/solid {"color":[80,60,40]}`, "GET /led/color"}) {
		t.Fatalf("unexpected hardware payload: %v", got.Actions)
	}
}

func TestJevCannotExecuteUnofferedOrRemovedCapability(t *testing.T) {
	for _, id := range []string{"scene_night", "led_off", "made_up_action", ""} {
		t.Run(id, func(t *testing.T) {
			calls := configureJevTest(t)
			Configure(map[string]bool{device.CapAudio: true})
			r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) { return id, nil })
			if got := MatchWithFallback(context.Background(), "This lamp is too bright.", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}); got != nil || calls.Load() != 0 {
				t.Fatalf("unoffered action executed: %+v", got)
			}
		})
	}
	t.Run("capability changed during request", func(t *testing.T) {
		calls := configureJevTest(t)
		r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) {
			Configure(map[string]bool{device.CapAudio: true})
			return "led_off", nil
		})
		if MatchWithFallback(context.Background(), "This lamp is too bright.", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}) != nil || calls.Load() != 0 {
			t.Fatal("removed capability executed")
		}
	})
}

func TestJevExecutionFailureRemainsHandled(t *testing.T) {
	routeIntentHAL(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) })
	Configure(map[string]bool{device.CapLight: true})
	r := fakeJevDecider(func(context.Context, string, string, []jev.Candidate) (string, error) { return "dim", nil })
	got := MatchWithFallback(context.Background(), "This lamp is too bright.", r, jev.Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"})
	if got == nil || !got.ExecutionFailed || got.Source != "jev" {
		t.Fatalf("lost failed execution: %+v", got)
	}
}
