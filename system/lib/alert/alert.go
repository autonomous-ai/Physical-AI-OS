// Package alert delivers maintainer/ops alerts about DEVICE ACTIONS to
// bff-campaign-service, which owns the Telegram bot token + destination chat.
//
// Data scope — this is important and load-bearing: these alerts report ONLY
// device actions and state changes (runtime switches, channel/connector setup
// outcomes, OAuth-refresh health, install/reset events, default-model swaps)
// plus device identity (label, MAC, SSID, IP, component versions). They NEVER
// carry end-customer content — no chat messages, no personal data. Purpose:
// product improvement and troubleshooting only.
//
// This is the autonomous-repo replacement for openclaw-lobster's lib/sendip,
// which POSTed straight to api.telegram.org with a hardcoded bot token. Because
// the autonomous repo is public, the token cannot live here; delivery is proxied
// through bff-campaign-service:
//
//	POST {config.LLMBaseURL}/alert   (LLMBaseURL already ends in /v1, so this is
//	                                  /api/v1/ai/v1/alert — no stripping)
//	Authorization: Bearer <config.LLMAPIKey>   (the device's lobster API key)
//
// Best-effort by design: every failure is logged, never propagated, so an alert
// path can never break the device action it is reporting on.
package alert

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/runtimereg"
	"go.autonomous.ai/os/system/server/config"
)

const (
	// maxMessageLen keeps the composed text under Telegram's 4096-char hard cap
	// with headroom (bff sends it verbatim via sendMessage).
	maxMessageLen = 3500
	// halVersionPath is where the HAL runtime records its version on disk.
	halVersionPath = "/opt/hal/VERSION_HAL"
)

// httpClient is shared; alerts are infrequent and best-effort.
var httpClient = &http.Client{Timeout: 10 * time.Second}

// payload is the POST body to bff-campaign-service. `message` is the fully
// composed text (title + device-info preamble + detail) — bff is a dumb relay.
type payload struct {
	DeviceID string `json:"device_id"`
	Type     string `json:"type"`
	Message  string `json:"message"`
	TS       int64  `json:"ts"`
}

// Notify posts an ops alert about a device action to bff-campaign-service.
// No-op (logged) when alerts are disabled or LLMBaseURL/LLMAPIKey are unset;
// all transport/HTTP errors are logged, never returned.
func Notify(ctx context.Context, cfg *config.Config, text string) {
	if cfg == nil || cfg.AlertsDisabled {
		return
	}
	base := strings.TrimRight(cfg.BackendBase(), "/")
	key := cfg.BackendKey()
	if base == "" || key == "" {
		slog.Debug("alert: skipped (base or key unset)", "component", "alert")
		return
	}
	if len(text) > maxMessageLen {
		text = text[:maxMessageLen]
	}

	body, err := json.Marshal(payload{
		DeviceID: cfg.DeviceID,
		Type:     "ops",
		Message:  text,
		TS:       time.Now().Unix(),
	})
	if err != nil {
		slog.Warn("alert: marshal failed", "component", "alert", "err", err)
		return
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/alert", bytes.NewReader(body))
	if err != nil {
		slog.Warn("alert: build request failed", "component", "alert", "err", err)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+key)

	resp, err := httpClient.Do(req)
	if err != nil {
		slog.Warn("alert: send failed", "component", "alert", "err", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		slog.Warn("alert: non-2xx", "component", "alert", "status", resp.StatusCode, "body", strings.TrimSpace(string(b)))
		return
	}
	slog.Info("alert sent", "component", "alert", "chars", len(text))
}

// Compose builds the standard alert body: a title line, the device-info
// preamble, and an optional detail block. Callers pass an emoji-prefixed title
// (e.g. "🟢 Runtime setup hermes.setup — SUCCESS") and, on failure, the error
// text as detail.
func Compose(cfg *config.Config, title, detail string) string {
	var b strings.Builder
	b.WriteString(title)
	b.WriteString("\n")
	b.WriteString(DeviceInfo(cfg))
	if strings.TrimSpace(detail) != "" {
		b.WriteString("\n")
		b.WriteString(detail)
	}
	return b.String()
}

// Notifyf composes a titled alert and sends it. Convenience for the common
// title+detail shape.
func Notifyf(ctx context.Context, cfg *config.Config, title, detail string) {
	Notify(ctx, cfg, Compose(cfg, title, detail))
}

// DeviceInfo builds the identity preamble (device metadata only — no customer
// data), formatted for readability like openclaw-lobster's sendip:
//
//	[<label>] <board model> <mac>
//	SSID: <ssid>, IP: <ip>
//	Versions: os=<osVer> runtime=<name>@<ver> hal=<halVer>
//	DeviceID: <backend device id>
//	ActiveAgent: <runtime>
//	FA: <fa mqtt channel>
//
// The human-readable hardware label (e.g. "intern-v2-7f72") leads, followed by
// the board model (Raspberry Pi / OrangePi …); the long backend device id goes
// on its own DeviceID line.
func DeviceInfo(cfg *config.Config) string {
	label := hardwareLabel()
	runtime, deviceID, faChannel, netSSID := "", "", "", ""
	if cfg != nil {
		runtime = strings.TrimSpace(cfg.AgentRuntime)
		deviceID = strings.TrimSpace(cfg.DeviceID)
		faChannel = strings.TrimSpace(cfg.FAChannel)
		netSSID = strings.TrimSpace(cfg.NetworkSSID)
	}
	if label == "" {
		label = deviceID
	}
	if label == "" {
		label = "unknown"
	}

	// SSID: prefer the configured network name (a required, always-set field on a
	// provisioned device); fall back to the live interface SSID. iwgetid is often
	// absent on OrangePi boards, which is why the live probe returns nothing.
	ssidVal := netSSID
	if ssidVal == "" {
		ssidVal = ssid()
	}

	var b strings.Builder
	head := "[" + label + "]"
	if board := boardModel(); board != "" {
		head += " " + board
	}
	fmt.Fprintf(&b, "%s %s\n", head, orNone(macAddress()))
	fmt.Fprintf(&b, "SSID: %s, IP: %s\n", orNone(ssidVal), orNone(ipAddr()))
	fmt.Fprintf(&b, "Versions: os=%s runtime=%s@%s hal=%s",
		orNone(config.OSVersion), orNone(runtime), orNone(runtimereg.Version(runtime)), orNone(halVersion()))
	if deviceID != "" {
		fmt.Fprintf(&b, "\nDeviceID: %s", deviceID)
	}
	if runtime != "" {
		fmt.Fprintf(&b, "\nActiveAgent: %s", runtime)
	}
	if faChannel != "" {
		fmt.Fprintf(&b, "\nFA: %s", faChannel)
	}
	return b.String()
}

// boardModel reads the board model string from the device tree (e.g. "Raspberry
// Pi 5 Model B", "OrangePi 4 LTS"). Empty on boards without it / non-Linux.
func boardModel() string {
	b, err := os.ReadFile("/proc/device-tree/model")
	if err != nil {
		return ""
	}
	return strings.TrimRight(strings.TrimSpace(string(b)), "\x00")
}

func orNone(s string) string {
	if strings.TrimSpace(s) == "" {
		return "(none)"
	}
	return s
}

// hardwareLabel mirrors device.GetDeviceMac's <device_type>-XXXX form without
// importing the heavy device service package (keeps this a leaf lib). Empty when
// not on a provisioned Pi.
func hardwareLabel() string {
	serial := readSerial()
	if serial == "" {
		return ""
	}
	suffix := serial
	if len(serial) > 4 {
		suffix = serial[len(serial)-4:]
	}
	deviceType := strings.ToLower(os.Getenv("DEVICE_TYPE"))
	if deviceType == "" {
		return ""
	}
	return deviceType + "-" + strings.ToLower(suffix)
}

func readSerial() string {
	if b, err := os.ReadFile("/proc/device-tree/serial-number"); err == nil {
		return strings.TrimSpace(strings.TrimRight(string(b), "\x00"))
	}
	for _, iface := range []string{"eth0", "end0"} {
		if b, err := os.ReadFile("/sys/class/net/" + iface + "/address"); err == nil {
			mac := strings.TrimSpace(string(b))
			if mac != "" && mac != "00:00:00:00:00:00" {
				return strings.ReplaceAll(mac, ":", "")
			}
		}
	}
	return ""
}

func macAddress() string {
	for _, iface := range []string{"wlan0", "eth0", "end0"} {
		if b, err := os.ReadFile("/sys/class/net/" + iface + "/address"); err == nil {
			if mac := strings.TrimSpace(string(b)); mac != "" {
				return mac
			}
		}
	}
	return ""
}

func ssid() string {
	out, err := exec.Command("iwgetid", "-r").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func ipAddr() string {
	out, err := exec.Command("hostname", "-I").Output()
	if err != nil {
		return ""
	}
	if f := strings.Fields(string(out)); len(f) > 0 {
		return f[0]
	}
	return ""
}

func halVersion() string {
	b, err := os.ReadFile(halVersionPath)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}
