// Package clocksync forces an NTP resync on devices without an RTC, which boot
// with a stale clock and fail TLS ("certificate is not yet valid") until NTP lands.
package clocksync

import (
	"context"
	"crypto/x509"
	"errors"
	"log/slog"
	"os/exec"
	"strings"
	"sync"
	"time"

	"go.autonomous.ai/os/system/lib/safego"
)

// runCmd runs a command and returns its combined output (a variable so tests can swap it).
var runCmd = func(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).CombinedOutput()
}

// pollInterval is how often Sync re-checks NTPSynchronized while it waits.
var pollInterval = time.Second

// kickWait caps one background resync started by Kick.
var kickWait = 90 * time.Second

// kickMinInterval rate-limits Kick; ping retries every 15s and must not spawn a sync each time.
var kickMinInterval = time.Minute

var (
	kickMu      sync.Mutex
	kickRunning bool
	lastKick    time.Time
)

// IsClockError reports whether err is a TLS verification failure caused by the
// certificate validity window (x509.Expired covers both "expired" and "not yet valid").
func IsClockError(err error) bool {
	var certErr x509.CertificateInvalidError
	return errors.As(err, &certErr) && certErr.Reason == x509.Expired
}

// Synchronized reports whether the kernel clock is NTP-synchronized.
func Synchronized(ctx context.Context) bool {
	out, err := runCmd(ctx, "timedatectl", "show", "-p", "NTPSynchronized", "--value")
	return err == nil && strings.TrimSpace(string(out)) == "yes"
}

// Sync asks the NTP client to poll now and waits up to wait for the clock to be
// synchronized. Images ship chrony or systemd-timesyncd; both are handled.
func Sync(ctx context.Context, wait time.Duration) bool {
	ctx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	if Synchronized(ctx) {
		return true
	}
	chrony := triggerChrony(ctx)
	if !chrony {
		if out, err := runCmd(ctx, "systemctl", "restart", "systemd-timesyncd"); err != nil {
			slog.Warn("systemd-timesyncd restart failed", "component", "clocksync", "error", err, "output", strings.TrimSpace(string(out)))
		}
	}
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
		if Synchronized(ctx) {
			if chrony {
				// Past chrony's makestep limit a big offset is slewed for hours; step it now.
				_, _ = runCmd(ctx, "chronyc", "makestep")
			}
			return true
		}
	}
}

// triggerChrony makes chronyd poll its sources now; false when chronyc is unavailable.
// "refresh" re-resolves pool names that failed while the device was offline (in
// AP mode), which otherwise retry with backoff and delayed sync by minutes.
// "makestep" alone is a no-op before chrony has a measurement.
func triggerChrony(ctx context.Context) bool {
	for _, args := range [][]string{{"online"}, {"refresh"}, {"burst", "4/4"}} {
		out, err := runCmd(ctx, "chronyc", args...)
		if errors.Is(err, exec.ErrNotFound) {
			return false
		}
		if err != nil {
			slog.Warn("chronyc command failed", "component", "clocksync", "command", args[0], "error", err, "output", strings.TrimSpace(string(out)))
		}
	}
	return true
}

// Kick starts a background Sync unless one is running or one started within
// kickMinInterval. Returns whether a sync was started.
func Kick(reason string) bool {
	kickMu.Lock()
	if kickRunning || (!lastKick.IsZero() && time.Since(lastKick) < kickMinInterval) {
		kickMu.Unlock()
		return false
	}
	kickRunning = true
	lastKick = time.Now()
	kickMu.Unlock()

	safego.Go("clocksync", func() {
		defer func() {
			kickMu.Lock()
			kickRunning = false
			kickMu.Unlock()
		}()
		slog.Info("clock resync started", "component", "clocksync", "reason", reason, "now", time.Now().UTC().Format(time.RFC3339))
		if Sync(context.Background(), kickWait) {
			slog.Info("clock synchronized", "component", "clocksync", "reason", reason, "now", time.Now().UTC().Format(time.RFC3339))
			return
		}
		slog.Warn("clock still not synchronized (is UDP 123 blocked?)", "component", "clocksync", "reason", reason, "wait", kickWait)
	})
	return true
}
