package accountlogin

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPromptFiltering(t *testing.T) {
	linked := (&Flow{Hosts: []string{"claude.ai"}, InputRequired: true}).ParsePrompt("\x1b]8;;https://claude.ai/oauth/authorize\x1b\\https://claude.ai/oauth/authorize\x1b]8;;\x1b\\")
	if linked.URL != "https://claude.ai/oauth/authorize" {
		t.Fatalf("OSC hyperlink lost: %+v", linked)
	}
	flow := &Flow{Hosts: []string{"auth.openai.com"}}
	p := flow.ParsePrompt("\x1b[32mhttps://auth.openai.com/codex/device\x1b[0m\nABCD-12345\nsecret sk-test-credential\nhttps://evil.invalid/authorize")
	if p.URL != "https://auth.openai.com/codex/device" || p.UserCode != "ABCD-12345" {
		t.Fatalf("unexpected safe prompt: %+v", p)
	}
	for _, u := range []string{"https://auth.openai.com.evil.invalid/oauth", "https://user@auth.openai.com/oauth", "https://auth.openai.com:123/oauth", "http://auth.openai.com/oauth", "https://auth.openai.com/help"} {
		if got := flow.ParsePrompt(u); got.URL != "" {
			t.Fatalf("unsafe URL accepted: %s", u)
		}
	}
	flow.InputRequired = true
	if p := flow.ParsePrompt("https://auth.openai.com/oauth ABCD-1234"); !p.InputRequired || p.UserCode != "" {
		t.Fatal("paste flow exposed unrelated device code")
	}
}
func TestNativeProcessInputAndCancellation(t *testing.T) {
	input := make(chan string, 1)
	flow := &Flow{Hosts: []string{"claude.ai"}, InputRequired: true, Command: exec.Command("sh", "-c", `printf 'https://claude.ai/oauth/authorize\n'; read code; test "$code" = 'code#state'`)}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	sent := false
	if err := flow.Run(ctx, func(p Prompt) {
		if !sent {
			input <- "code#state"
			sent = true
		}
	}, input); err != nil {
		t.Fatal(err)
	}
	if !sent {
		t.Fatal("prompt was not delivered")
	}
	ctx, cancel = context.WithCancel(context.Background())
	flow.Command = exec.Command("sh", "-c", `printf 'https://claude.ai/oauth/authorize\n'; read code`)
	done := make(chan error, 1)
	go func() { done <- flow.Run(ctx, func(Prompt) { cancel() }, make(chan string)) }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancelled login succeeded")
		}
	case <-time.After(3 * time.Second):
		t.Fatal("login process did not stop")
	}
}
func TestEnvironmentAndFileRollback(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "must-not-inherit")
	t.Setenv("OPENAI_BASE_URL", "must-not-inherit")
	t.Setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-inherit")
	if env := strings.Join(cleanEnv("/staged", nil), "\n"); strings.Contains(env, "must-not-inherit") {
		t.Fatal("inherited authentication")
	}
	dir := t.TempDir()
	a, b := filepath.Join(dir, "a"), filepath.Join(dir, "b")
	if err := os.WriteFile(a, []byte("old"), 0640); err != nil {
		t.Fatal(err)
	}
	rollback, err := writeChanges(map[string][]byte{a: []byte("new"), b: []byte("new-account")})
	if err != nil {
		t.Fatal(err)
	}
	if info, _ := os.Stat(b); info.Mode().Perm() != 0600 {
		t.Fatal("credential permissions")
	}
	if err = rollback(); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(a)
	if string(data) != "old" {
		t.Fatal("original not restored")
	}
	if _, err = os.Stat(b); !os.IsNotExist(err) {
		t.Fatal("new credential not removed")
	}
}
