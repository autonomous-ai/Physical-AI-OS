package claudecode

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

// drainEvents runs scanLoginOutput on transcript and returns the scan outcome
// plus every event it emitted.
func drainEvents(t *testing.T, transcript string) (loginScan, []domain.PairingEvent) {
	t.Helper()
	ch := make(chan domain.PairingEvent, 16)
	scan := scanLoginOutput(strings.NewReader(transcript), ch)
	close(ch)
	var events []domain.PairingEvent
	for evt := range ch {
		events = append(events, evt)
	}
	return scan, events
}

func TestScanLoginOutputEmitsURLOnce(t *testing.T) {
	transcript := "Opening browser...\r\n" +
		"Visit: https://claude.ai/oauth/authorize?code=true&client_id=abc123\r\n" +
		"If the browser did not open, visit https://claude.ai/oauth/authorize?code=true&client_id=abc123\r\n" +
		"Paste code here:\r\n"

	scan, events := drainEvents(t, transcript)
	if len(events) != 1 {
		t.Fatalf("events = %d, want exactly 1 pairing_url (URL must be emitted once)", len(events))
	}
	if events[0].Status != domain.PairingStatusURL {
		t.Errorf("status = %q, want %q", events[0].Status, domain.PairingStatusURL)
	}
	if want := "https://claude.ai/oauth/authorize?code=true&client_id=abc123"; events[0].URL != want {
		t.Errorf("url = %q, want %q", events[0].URL, want)
	}
	if scan.token != "" || scan.sawSuccess {
		t.Errorf("scan = %+v, want no token / no success before the code is exchanged", scan)
	}
}

func TestScanLoginOutputExtractsToken(t *testing.T) {
	transcript := "Visit: https://claude.ai/oauth/authorize?client_id=x\n" +
		"Paste code here: abc\n" +
		"Your long-lived token: sk-ant-oat01-Abc123_def-456\n" +
		"Store this token securely.\n"

	scan, events := drainEvents(t, transcript)
	if scan.token != "sk-ant-oat01-Abc123_def-456" {
		t.Errorf("token = %q, want the sk-ant-oat01 token", scan.token)
	}
	if !scan.sawSuccess {
		t.Error("sawSuccess = false, want true when a token was printed")
	}
	if len(events) != 1 || events[0].Status != domain.PairingStatusURL {
		t.Errorf("events = %+v, want exactly the pairing_url event", events)
	}
}

func TestScanLoginOutputStripsANSIAndSplitsCR(t *testing.T) {
	// Interactive CLIs redraw with bare \r and wrap output in ANSI escapes —
	// the scanner must still find the URL and token.
	transcript := "\x1b[2K\rConnecting…\r" +
		"\x1b[32mVisit:\x1b[0m https://claude.ai/oauth/authorize?client_id=y\r" +
		"\x1b[1A\x1b[2K✔ Successfully logged in\r" +
		"\x1b[33msk-ant-oat01-zzz\x1b[0m\r"

	scan, events := drainEvents(t, transcript)
	if len(events) != 1 || events[0].Status != domain.PairingStatusURL {
		t.Fatalf("events = %+v, want one pairing_url", events)
	}
	if events[0].URL != "https://claude.ai/oauth/authorize?client_id=y" {
		t.Errorf("url = %q — ANSI escapes must not leak into the match", events[0].URL)
	}
	if scan.token != "sk-ant-oat01-zzz" {
		t.Errorf("token = %q, want sk-ant-oat01-zzz", scan.token)
	}
	if !scan.sawSuccess {
		t.Error("sawSuccess = false, want true")
	}
}

func TestScanLoginOutputSuccessMarkerWithoutToken(t *testing.T) {
	transcript := "Visit: https://claude.ai/oauth/authorize\n" +
		"Login successful. Credentials saved.\n"

	scan, _ := drainEvents(t, transcript)
	if scan.token != "" {
		t.Errorf("token = %q, want empty", scan.token)
	}
	if !scan.sawSuccess {
		t.Error("sawSuccess = false, want true on the textual success marker")
	}
}

func TestScanLoginOutputFailureKeepsLastLine(t *testing.T) {
	transcript := "Visit: https://claude.ai/oauth/authorize\n" +
		"Error: invalid authorization code\n"

	scan, _ := drainEvents(t, transcript)
	if scan.sawSuccess || scan.token != "" {
		t.Errorf("scan = %+v, want no success/token", scan)
	}
	if scan.lastLine != "Error: invalid authorization code" {
		t.Errorf("lastLine = %q — the failure path surfaces the CLI's last output", scan.lastLine)
	}
}

func TestSubmitClaudeLoginCode(t *testing.T) {
	s := &ClaudeCodeService{}

	if err := s.SubmitClaudeLoginCode("abc"); err == nil {
		t.Error("SubmitClaudeLoginCode with no active login: err = nil, want error")
	}
	// Empty code → error, even with a flow active.
	var sink strings.Builder
	claudeLoginMu.Lock()
	claudeLoginStdin = &sink
	claudeLoginMu.Unlock()
	defer func() {
		claudeLoginMu.Lock()
		claudeLoginStdin = nil
		claudeLoginMu.Unlock()
	}()
	if err := s.SubmitClaudeLoginCode("   "); err == nil {
		t.Error("SubmitClaudeLoginCode(blank): err = nil, want error")
	}
	if err := s.SubmitClaudeLoginCode(" code-123 \n"); err != nil {
		t.Fatalf("SubmitClaudeLoginCode: %v", err)
	}
	if got := sink.String(); got != "code-123\r" {
		t.Errorf("written = %q, want %q", got, "code-123\r")
	}
}

func TestStartClaudeLoginSingleFlight(t *testing.T) {
	// Simulate an in-flight login; a second Start must fail fast with a
	// one-event channel (mirrors whatsapp's pairing_already_in_progress).
	claudeLoginMu.Lock()
	claudeLoginActive = true
	claudeLoginMu.Unlock()
	defer func() {
		claudeLoginMu.Lock()
		claudeLoginActive = false
		claudeLoginMu.Unlock()
	}()

	s := &ClaudeCodeService{}
	ch := s.StartClaudeLogin(t.Context())
	evt, ok := <-ch
	if !ok {
		t.Fatal("channel closed without an event")
	}
	if evt.Status != domain.PairingStatusFailure || evt.Error != "login_already_in_progress" {
		t.Errorf("evt = %+v, want failure/login_already_in_progress", evt)
	}
	if _, ok := <-ch; ok {
		t.Error("channel not closed after the one-shot failure event")
	}
}

func TestSplitPTYLines(t *testing.T) {
	ch := make(chan domain.PairingEvent, 4)
	scan := scanLoginOutput(strings.NewReader("a\rb\nc"), ch)
	close(ch)
	if scan.lastLine != "c" {
		t.Errorf("lastLine = %q, want %q (EOF fragment must be scanned)", scan.lastLine, "c")
	}
}
