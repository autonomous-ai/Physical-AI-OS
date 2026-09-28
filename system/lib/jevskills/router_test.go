package jevskills

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func fixture(t *testing.T, handler http.HandlerFunc) (*Router, string, *atomic.Int32) {
	t.Helper()
	calls := new(atomic.Int32)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls.Add(1); handler(w, r) }))
	t.Cleanup(server.Close)
	root := t.TempDir()
	config := filepath.Join(root, "config.json")
	data, _ := json.Marshal(map[string]string{"llm_base_url": server.URL, "llm_api_key": "mock-secret"})
	if err := os.WriteFile(config, data, 0600); err != nil {
		t.Fatal(err)
	}
	skills := filepath.Join(root, "skills")
	writeSkill(t, skills, "lamp", "---\nname: lamp\ndescription: Control the lamp\n---\nUse the existing lamp tool.\n")
	return New(Options{Runtime: "test", ConfigPath: config, SkillsDir: skills}), skills, calls
}

func writeSkill(t *testing.T, root, name, content string) {
	t.Helper()
	dir := filepath.Join(root, name)
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "SKILL.md"), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
}

func decision(choice string, probabilities map[string]float64, fits ...float64) []byte {
	answers := map[string]any{"skill": map[string]any{"type": "choice", "choice": choice, "probabilities": probabilities}}
	for i, fit := range fits {
		answers[fmt.Sprintf("fit_skill_%d", i)] = map[string]any{"type": "noul", "noul": fit}
	}
	data, _ := json.Marshal(map[string]any{"answers": answers})
	return data
}

func accepted(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write(decision("skill_0", map[string]float64{"none": .05, "skill_0": .95}, .95))
}

func TestPreloadWireAndTurnIsolation(t *testing.T) {
	var prompts []string
	r, root, calls := fixture(t, func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/jev/decisions" || req.Header.Get("Authorization") != "Bearer mock-secret" || req.Header.Get("User-Agent") != "AutonomousOS-Jev/0.1" {
			t.Error("wrong request contract")
		}
		var body struct {
			Model string
			State struct {
				Prompt     string
				Candidates []candidate
			}
			Questions map[string]question
		}
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != "typesafe/jev-1.13" || len(body.Questions) != 2 || body.Questions["skill"].Type != "choice" || body.Questions["fit_skill_0"].Type != "noul" {
			t.Error("wrong Decisions schema")
		}
		prompts = append(prompts, body.State.Prompt)
		if body.State.Prompt == "hello" {
			_, _ = w.Write(decision("none", map[string]float64{"none": .95, "skill_0": .05}, .05))
			return
		}
		accepted(w, req)
	})
	got := r.Context(context.Background(), "dim this lamp")
	if !strings.Contains(got, "Use the existing lamp tool.") || !strings.Contains(got, filepath.Join(root, "lamp")) || !strings.Contains(got, "not permission") {
		t.Fatalf("missing complete preload: %s", got)
	}
	if got = r.Context(context.Background(), "hello"); got != "" {
		t.Fatal("selection leaked to next turn")
	}
	if calls.Load() != 2 || len(prompts) != 2 || prompts[1] != "hello" {
		t.Fatal("history or preload sent to classifier")
	}
}

func TestStrictDecisionParity(t *testing.T) {
	for _, tc := range []struct {
		name    string
		data    []byte
		want    int
		invalid bool
	}{
		{"accepted", decision("skill_0", map[string]float64{"none": .1, "skill_0": .9}, .9), 0, false},
		{"none", decision("none", map[string]float64{"none": .9, "skill_0": .1}, .9), -1, false},
		{"low_choice", decision("skill_0", map[string]float64{"none": .35, "skill_0": .65}, .9), -1, false},
		{"low_fit", decision("skill_0", map[string]float64{"none": .1, "skill_0": .9}, .59), -1, false},
		{"invented", decision("other", map[string]float64{"none": .1, "other": .9}, .9), -1, true},
		{"wrong_sum", decision("skill_0", map[string]float64{"none": .4, "skill_0": .9}, .9), -1, true},
		{"not_winner", decision("skill_0", map[string]float64{"none": .9, "skill_0": .1}, .9), -1, true},
		{"missing_fit", decision("skill_0", map[string]float64{"none": .1, "skill_0": .9}), -1, true},
		{"null", []byte(`{"answers":{"skill":{"type":"choice","choice":"skill_0","probabilities":{"none":null,"skill_0":1}},"fit_skill_0":{"type":"noul","noul":1}}}`), -1, true},
		{"provider_error", []byte(`{"error":null}`), -1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := parseDecision(tc.data, 1)
			if got != tc.want || (err != nil) != tc.invalid {
				t.Fatalf("got %d,%v", got, err)
			}
		})
	}
}

func TestSkipInputsDisabledAndPolicy(t *testing.T) {
	r, _, calls := fixture(t, accepted)
	for _, message := range []string{"", "/help", "[system] update", "[sensing:presence.enter] hello", "[HANDLED] prior", "use [skills:lamp]", "brighter", "Make it brighter.", "continue", "tiếp tục", strings.Repeat("a", 8001)} {
		if r.Context(context.Background(), message) != "" {
			t.Fatal("ineligible input selected")
		}
	}
	r.opts.Disabled = true
	r.Context(context.Background(), "dim light")
	r.opts.Disabled = false
	r.opts.Eligible = func(string, map[string]any) bool { return false }
	r.Context(context.Background(), "dim light")
	if calls.Load() != 0 {
		t.Fatal("ineligible request reached provider")
	}
}

func TestTimeoutBusyAndCooldown(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	r, _, calls := fixture(t, func(w http.ResponseWriter, req *http.Request) { close(entered); <-release; accepted(w, req) })
	r.opts.Timeout = 40 * time.Millisecond
	done := make(chan string, 1)
	go func() { done <- r.Context(context.Background(), "dim light") }()
	<-entered
	if r.Context(context.Background(), "another") != "" {
		t.Fatal("busy call must skip")
	}
	select {
	case out := <-done:
		if out != "" {
			t.Fatal("late selection accepted")
		}
	case <-time.After(time.Second):
		t.Fatal("budget not enforced")
	}
	close(release)
	// Wait for worker completion without inspecting state concurrently.
	r.mu.Lock()
	r.mu.Unlock()
	if r.Context(context.Background(), "again") != "" || calls.Load() != 1 {
		t.Fatal("cooldown missing")
	}
}

func TestSkillChangeDuringInference(t *testing.T) {
	var root string
	r, dir, _ := fixture(t, func(w http.ResponseWriter, req *http.Request) {
		writeSkill(t, root, "lamp", "---\nname: lamp\ndescription: changed\n---\nDifferent instructions\n")
		accepted(w, req)
	})
	root = dir
	if got := r.Context(context.Background(), "dim light"); got != "" {
		t.Fatal("changed skill preloaded")
	}
}

func TestNetworkFailuresFailOpen(t *testing.T) {
	for _, mode := range []string{"status", "redirect", "oversize", "malformed"} {
		t.Run(mode, func(t *testing.T) {
			redirectCalls := new(atomic.Int32)
			target := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { redirectCalls.Add(1) }))
			defer target.Close()
			r, _, calls := fixture(t, func(w http.ResponseWriter, req *http.Request) {
				switch mode {
				case "status":
					w.WriteHeader(503)
				case "redirect":
					http.Redirect(w, req, target.URL, 302)
				case "oversize":
					_, _ = w.Write([]byte(strings.Repeat("x", maxResponse+1)))
				case "malformed":
					_, _ = w.Write([]byte(`{}`))
				}
			})
			if r.Context(context.Background(), "dim light") != "" {
				t.Fatal("error yielded context")
			}
			r.Context(context.Background(), "again")
			if calls.Load() != 1 || redirectCalls.Load() != 0 {
				t.Fatal("retry or credential redirect")
			}
		})
	}
}

func TestCatalogConservativeLimits(t *testing.T) {
	r, root, _ := fixture(t, accepted)
	for i, front := range []string{"disable-model-invocation: true", "user-invocable: false", "allowed-tools: Bash", "metadata: {requires: {bins: [missing]}}", "context: fork", "unknown-policy: value"} {
		writeSkill(t, root, fmt.Sprintf("skip%d", i), fmt.Sprintf("---\nname: skip%d\ndescription: excluded\n%s\n---\nBody\n", i, front))
	}
	writeSkill(t, root, "dynamic", "---\nname: dynamic\ndescription: excluded\n---\n!`touch forbidden`\n")
	if err := os.MkdirAll(filepath.Join(root, "linked"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "lamp", "SKILL.md"), filepath.Join(root, "linked", "SKILL.md")); err != nil {
		t.Fatal(err)
	}
	items, err := r.catalog(context.Background())
	if err != nil || len(items) != 1 {
		t.Fatalf("unsafe catalog: %d %v", len(items), err)
	}
	for i := 0; i < 32; i++ {
		writeSkill(t, root, fmt.Sprintf("extra%d", i), fmt.Sprintf("---\nname: extra%d\ndescription: okay\n---\nBody\n", i))
	}
	if _, err = r.catalog(context.Background()); err == nil {
		t.Fatal("must not truncate >32 eligible skills")
	}
}

func TestEndpointCredentialBoundary(t *testing.T) {
	for _, url := range []string{"http://remote.test/jev/decisions", "https://user:pass@host/x", "https://host/x?foo=1", "https://host/x#frag"} {
		if validEndpoint(url) {
			t.Errorf("accepted %s", url)
		}
	}
	for _, url := range []string{"https://proxy.test/jev/decisions", "http://127.0.0.1/x", "http://[::1]/x"} {
		if !validEndpoint(url) {
			t.Errorf("rejected %s", url)
		}
	}
}

func TestCatalogRefusesSpecialFilesAndOutsideLinks(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	writeSkill(t, outside, "private", "---\nname: private\ndescription: private\n---\nDo not load\n")
	if err := os.Symlink(filepath.Join(outside, "private"), filepath.Join(root, "outside")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "pipe"), 0700); err != nil {
		t.Fatal(err)
	}
	pipe := filepath.Join(root, "pipe", "SKILL.md")
	if err := syscall.Mkfifo(pipe, 0600); err != nil {
		t.Fatal(err)
	}
	r := New(Options{SkillsDir: root})
	done := make(chan error, 1)
	go func() {
		items, err := r.catalog(context.Background())
		if len(items) != 0 {
			err = fmt.Errorf("unexpected skills: %d", len(items))
		}
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("catalog blocked on FIFO")
	}
	if _, err := ReadPolicyFile(pipe); err == nil {
		t.Fatal("policy accepted FIFO")
	}
	if _, err := ReadPolicyFile(filepath.Join(root, "missing")); !os.IsNotExist(err) {
		t.Fatalf("lost absence: %v", err)
	}
}

func TestAuthoritativeVoiceSelectionAndContextualAbstention(t *testing.T) {
	var received string
	r, _, calls := fixture(t, func(w http.ResponseWriter, req *http.Request) {
		var body struct{ State struct{ Prompt string } }
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		received = body.State.Prompt
		accepted(w, req)
	})
	for _, message := range []string{
		"[user] [voice-instruction] brighter [transcript] make the lamp brighter",
		"[user] [voice-instruction] continue [transcript] turn on the lamp",
		"[user] [voice-instruction] [transcript] turn on the lamp",
		"[user] [voice-instruction] brighter [voice-instruction] turn on the lamp",
		"[user] brighter\n[harness-reply run_id=lamp-render channel=voice]",
		"[user] continue\n[harness-reply run_id=lamp-render channel=web]\n[system-context: previous result] turn on the lamp",
		"[user] /help",
	} {
		if got := r.Context(context.Background(), message); got != "" {
			t.Errorf("contextual/invalid envelope selected: %q", message)
		}
	}
	if calls.Load() != 0 {
		t.Fatal("contextual voice followup reached provider")
	}
	original := "[user] [voice-instruction] turn off the lamp [transcript] turn on the speaker\n[harness-reply run_id=run-1 channel=voice]\n[system-context: previous result] brighter image"
	if got := r.Context(context.Background(), original); got == "" {
		t.Fatal("explicit authoritative request was not eligible")
	}
	if calls.Load() != 1 || received != "turn off the lamp" {
		t.Fatalf("classifier received transcript or routing metadata: calls=%d prompt=%q", calls.Load(), received)
	}
}
