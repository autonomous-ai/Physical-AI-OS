// Package alert sends best-effort ops alerts about device actions via the backend's /alert relay.
// Alerts carry only device actions and identity, never end-customer content.
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
	// maxMessageLen stays under Telegram's 4096-char cap with headroom.
	maxMessageLen = 3500
	// halVersionPath is where the HAL runtime records its version on disk.
	halVersionPath = "/opt/hal/VERSION_HAL"
)

// httpClient is shared; alerts are infrequent and best-effort.
var httpClient = &http.Client{Timeout: 10 * time.Second}

// payload is the POST body to the alert relay; Message is the fully composed text.
type payload struct {
	DeviceID string `json:"device_id"`
	Type     string `json:"type"`
	Message  string `json:"message"`
	TS       int64  `json:"ts"`
}

// Notify posts an ops alert; errors and missing config are logged, never returned.
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

// Compose builds the alert body: title line, device-info preamble and optional detail.
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

// Notifyf composes a titled alert and sends it.
func Notifyf(ctx context.Context, cfg *config.Config, title, detail string) {
	Notify(ctx, cfg, Compose(cfg, title, detail))
}

// DeviceInfo builds the device identity preamble (metadata only, no customer data).
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

	// Prefer the configured SSID; iwgetid is often absent on OrangePi boards.
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

// boardModel reads the device-tree board model, or "" when unavailable.
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

// hardwareLabel mirrors device.GetDeviceMac's <device_type>-XXXX form; "" when unprovisioned.
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
