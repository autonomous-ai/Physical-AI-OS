package network

import (
	"context"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

const (
	wifiLossGrace       = 90 * time.Second
	wifiAPRetryInterval = 2 * time.Minute
	wifiJoinTimeout     = 45 * time.Second
)

// wifiRecovery is driven by the monitor under operationMu; scripts own AP/STA transitions.
type wifiRecovery struct {
	lostSince time.Time
	retryAt   time.Time
	joinUntil time.Time
	run       func(context.Context, string, ...string) ([]byte, error)
}

func (r *wifiRecovery) command(ctx context.Context, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, wifiJoinTimeout)
	defer cancel()
	if r.run != nil {
		return r.run(ctx, name, args...)
	}
	cmd := exec.CommandContext(ctx, name, args...)
	// Kill the whole process group before another operation owns the interface.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error {
		err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		if err == syscall.ESRCH {
			return os.ErrProcessDone
		}
		return err
	}
	cmd.WaitDelay = time.Second
	return cmd.CombinedOutput()
}

func (r *wifiRecovery) tick(ctx context.Context, now time.Time, ssid, password string) {
	// A failed probe during our own join must not leave the hotspot down forever.
	defer func() {
		if !r.joinUntil.IsZero() && !now.Before(r.joinUntil) {
			r.startAP(ctx, now)
		}
	}()
	info, err := r.command(ctx, "iw", "dev", wifiInterface, "info")
	if err != nil {
		// Outside our pending join, an unknown state must not switch modes.
		return
	}
	ap := false
	managed := false
	for _, line := range strings.Split(string(info), "\n") {
		ap = ap || strings.TrimSpace(line) == "type AP"
		managed = managed || strings.TrimSpace(line) == "type managed"
	}
	if !ap && !managed {
		return
	}
	if ap {
		r.lostSince = time.Time{}
		r.joinUntil = time.Time{}
		if r.retryAt.IsZero() {
			r.retryAt = now.Add(wifiAPRetryInterval)
		}
		if now.Before(r.retryAt) {
			return
		}
		stations, err := r.command(ctx, "iw", "dev", wifiInterface, "station", "dump")
		// Don't interrupt setup: defer on probe failure or connected clients.
		if err != nil || strings.TrimSpace(string(stations)) != "" {
			return
		}
		if ctx.Err() != nil {
			return
		}
		slog.Info("retrying saved WiFi from recovery hotspot", "component", "network-monitor")
		args := []string{ssid}
		if password != "" {
			args = append(args, password)
		}
		// Rewrite from saved config; never log the command output (contains credentials).
		started := time.Now()
		if _, err := r.command(ctx, "connect-wifi", args...); err != nil {
			r.startAP(ctx, now)
			return
		}
		r.joinUntil = now.Add(time.Since(started) + wifiJoinTimeout)
		r.retryAt = time.Time{}
		return
	}
	link, err := r.command(ctx, "iw", "dev", wifiInterface, "link")
	if err != nil {
		return
	}
	addresses, err := r.command(ctx, "ip", "-4", "-o", "addr", "show", "dev", wifiInterface, "scope", "global")
	if err != nil {
		return
	}
	if stationLinkedTo(string(link), ssid) && hasStationIPv4(string(addresses)) {
		if !r.joinUntil.IsZero() || !r.lostSince.IsZero() {
			slog.Info("WiFi link recovered", "component", "network-monitor")
			// Some images only restart Avahi in the AP script.
			_, _ = r.command(ctx, "systemctl", "restart", "avahi-daemon")
		}
		r.lostSince, r.retryAt, r.joinUntil = time.Time{}, time.Time{}, time.Time{}
		return
	}
	if !r.joinUntil.IsZero() {
		return
	}
	// wpa_supplicant keeps retrying during this grace period.
	if r.lostSince.IsZero() {
		r.lostSince = now
	}
	if now.Sub(r.lostSince) >= wifiLossGrace {
		r.startAP(ctx, now)
	}
}

func (r *wifiRecovery) startAP(ctx context.Context, now time.Time) {
	// Restore the hotspot before releasing operationMu, even on cancellation.
	if ctx.Err() != nil {
		ctx = context.Background()
	}
	if _, err := r.command(ctx, "/usr/local/bin/device-ap-mode"); err != nil {
		slog.Error("recovery hotspot failed", "component", "network-monitor", "error", err)
		return
	}
	r.lostSince, r.joinUntil = time.Time{}, time.Time{}
	r.retryAt = now.Add(wifiAPRetryInterval)
	slog.Info("recovery hotspot enabled", "component", "network-monitor")
}

func hasStationIPv4(out string) bool {
	fields := strings.Fields(out)
	for i, field := range fields {
		if field != "inet" || i+1 == len(fields) {
			continue
		}
		ip, _, err := net.ParseCIDR(fields[i+1])
		if err == nil && ip.To4() != nil && ip.IsGlobalUnicast() && !ip.IsLinkLocalUnicast() && !ip.Equal(net.ParseIP("192.168.100.1")) {
			return true
		}
	}
	return false
}

func stationLinkedTo(link, ssid string) bool {
	if !strings.HasPrefix(strings.TrimSpace(link), "Connected to ") {
		return false
	}
	for _, line := range strings.Split(link, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "SSID:") {
			return decodeIWSSIDEscape(strings.TrimSpace(strings.TrimPrefix(line, "SSID:"))) == ssid
		}
	}
	return false
}
