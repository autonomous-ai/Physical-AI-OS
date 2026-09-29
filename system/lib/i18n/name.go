package i18n

import (
	"strings"
	"sync"
)

// Device name placeholders: {name} (lowercase, matchers) and {Name} (title case, spoken text).
var (
	deviceNameMu      sync.RWMutex
	deviceNameLower   string
	deviceNameDisplay string
)

// SetDeviceName sets the {name}/{Name} value and rebuilds the wake-word strip list; "" is ignored.
func SetDeviceName(name string) {
	n := strings.ToLower(strings.TrimSpace(name))
	if n == "" {
		return
	}
	disp := strings.ToUpper(n[:1]) + n[1:]
	deviceNameMu.Lock()
	deviceNameLower = n
	deviceNameDisplay = disp
	deviceNameMu.Unlock()
	SetChitchatWakeWords(BuildChitchatWakeWords(n))
}

// DeviceName returns the current lowercase device/agent name.
func DeviceName() string {
	deviceNameMu.RLock()
	defer deviceNameMu.RUnlock()
	return deviceNameLower
}

// applyName fills {Name}/{name} placeholders with the current device name.
func applyName(s string) string {
	deviceNameMu.RLock()
	lower, disp := deviceNameLower, deviceNameDisplay
	deviceNameMu.RUnlock()
	if disp != "" {
		s = strings.ReplaceAll(s, "{Name}", disp)
	}
	if lower != "" {
		s = strings.ReplaceAll(s, "{name}", lower)
	}
	return s
}

// applyNameAll applies applyName to each element; nil stays nil.
func applyNameAll(in []string) []string {
	if in == nil {
		return nil
	}
	out := make([]string, len(in))
	for i, s := range in {
		out[i] = applyName(s)
	}
	return out
}
