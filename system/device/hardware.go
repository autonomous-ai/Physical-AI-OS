package device

import (
	"os"
	"regexp"
	"strings"
)

// GetDeviceMac returns the hardware ID as <device_type>-XXXX (last 4 chars of the
// serial or eth MAC), matching setup.sh; empty if unavailable.
func GetDeviceMac() string {
	serial := readSerial()
	if serial == "" {
		return ""
	}
	suffix := serial
	if len(serial) > 4 {
		suffix = serial[len(serial)-4:]
	}
	// Must match the mDNS hostname / AP SSID from setup.sh; no fallback device type.
	deviceType := strings.ToLower(os.Getenv("DEVICE_TYPE"))
	if deviceType == "" {
		return ""
	}
	return deviceType + "-" + strings.ToLower(suffix)
}

func readSerial() string {
	// Pi 5: device-tree
	if b, err := os.ReadFile("/proc/device-tree/serial-number"); err == nil {
		return strings.TrimSpace(strings.TrimRight(string(b), "\x00"))
	}
	// Pi 4: cpuinfo
	if b, err := os.ReadFile("/proc/cpuinfo"); err == nil {
		re := regexp.MustCompile(`(?m)^Serial\s*:\s*(\S+)`)
		if m := re.FindSubmatch(b); len(m) >= 2 {
			return strings.TrimSpace(string(m[1]))
		}
	}
	// Non-Pi boards (e.g. OrangePi): fall back to the eth0/end0 MAC.
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
