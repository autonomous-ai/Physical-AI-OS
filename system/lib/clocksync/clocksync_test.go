package clocksync

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/url"
	"os/exec"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeRunner records commands and reports NTPSynchronized=yes after syncAfter checks.
type fakeRunner struct {
	mu        sync.Mutex
	calls     []string
	checks    int
	syncAfter int
	noChrony  bool
}

func (f *fakeRunner) run(_ context.Context, name string, args ...string) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cmd := strings.TrimSpace(name + " " + strings.Join(args, " "))
	f.calls = append(f.calls, cmd)
	switch {
	case name == "timedatectl":
		f.checks++
		if f.syncAfter >= 0 && f.checks > f.syncAfter {
			return []byte("yes\n"), nil
		}
		return []byte("no\n"), nil
	case name == "chronyc" && f.noChrony:
		return nil, &exec.Error{Name: name, Err: exec.ErrNotFound}
	}
	return nil, nil
}

func (f *fakeRunner) commands() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for _, c := range f.calls {
		if !strings.HasPrefix(c, "timedatectl") {
			out = append(out, c)
		}
	}
	return out
}

func useFake(t *testing.T, f *fakeRunner) {
	t.Helper()
	oldRun, oldPoll := runCmd, pollInterval
	runCmd, pollInterval = f.run, time.Millisecond
	t.Cleanup(func() { runCmd, pollInterval = oldRun, oldPoll })
}

func TestIsClockError(t *testing.T) {
	notYetValid := x509.CertificateInvalidError{Reason: x509.Expired, Detail: "current time 2026-04-27T20:01:46Z is before 2026-07-06T00:00:00Z"}
	wrapped := &url.Error{Op: "Post", URL: "https://example.com/ping", Err: &tls.CertificateVerificationError{Err: notYetValid}}
	if !IsClockError(fmt.Errorf("request: %w", wrapped)) {
		t.Fatal("not-yet-valid certificate behind url.Error should be a clock error")
	}
	if IsClockError(&tls.CertificateVerificationError{Err: x509.UnknownAuthorityError{}}) {
		t.Fatal("unknown authority is not a clock error")
	}
	if IsClockError(x509.CertificateInvalidError{Reason: x509.NotAuthorizedToSign}) {
		t.Fatal("other invalid-certificate reasons are not clock errors")
	}
	if IsClockError(errors.New("i/o timeout")) || IsClockError(nil) {
		t.Fatal("non-certificate errors are not clock errors")
	}
}

func TestSyncAlreadySynchronizedDoesNothing(t *testing.T) {
	f := &fakeRunner{syncAfter: 0}
	useFake(t, f)
	if !Sync(context.Background(), time.Second) {
		t.Fatal("Sync should report synchronized")
	}
	if got := f.commands(); len(got) != 0 {
		t.Fatalf("no NTP commands expected, got %v", got)
	}
}

func TestSyncChronyRefreshesThenSteps(t *testing.T) {
	f := &fakeRunner{syncAfter: 3}
	useFake(t, f)
	if !Sync(context.Background(), time.Second) {
		t.Fatal("Sync should report synchronized")
	}
	want := []string{"chronyc online", "chronyc refresh", "chronyc burst 4/4", "chronyc makestep"}
	if got := f.commands(); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("commands = %v, want %v", got, want)
	}
}

func TestSyncFallsBackToTimesyncd(t *testing.T) {
	f := &fakeRunner{syncAfter: 2, noChrony: true}
	useFake(t, f)
	if !Sync(context.Background(), time.Second) {
		t.Fatal("Sync should report synchronized")
	}
	want := []string{"chronyc online", "systemctl restart systemd-timesyncd"}
	if got := f.commands(); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("commands = %v, want %v", got, want)
	}
}

func TestSyncTimesOut(t *testing.T) {
	f := &fakeRunner{syncAfter: -1}
	useFake(t, f)
	if Sync(context.Background(), 20*time.Millisecond) {
		t.Fatal("Sync should time out when NTP never synchronizes")
	}
	for _, c := range f.commands() {
		if c == "chronyc makestep" {
			t.Fatal("makestep must not run before chrony is synchronized")
		}
	}
}

func TestKickRateLimited(t *testing.T) {
	f := &fakeRunner{syncAfter: 0}
	useFake(t, f)
	oldMin := kickMinInterval
	kickMinInterval = time.Hour
	kickMu.Lock()
	kickRunning, lastKick = false, time.Time{}
	kickMu.Unlock()
	t.Cleanup(func() {
		kickMinInterval = oldMin
		kickMu.Lock()
		kickRunning, lastKick = false, time.Time{}
		kickMu.Unlock()
	})

	if !Kick("test") {
		t.Fatal("first Kick should start a sync")
	}
	if Kick("test") {
		t.Fatal("second Kick within kickMinInterval should be skipped")
	}
	deadline := time.Now().Add(time.Second)
	for {
		kickMu.Lock()
		running := kickRunning
		kickMu.Unlock()
		if !running {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("background sync did not finish")
		}
		time.Sleep(time.Millisecond)
	}
	if Kick("test") {
		t.Fatal("Kick after a finished sync is still rate-limited")
	}
}
