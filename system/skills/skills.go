// Package skills holds the platform skill catalog and each skill's required
// hardware capability, independent of the agentic runtime.
package skills

import "go.autonomous.ai/os/system/device"

// Catalog and Capability are generated into catalog_gen.go by `make skills-catalog`.

// Supported returns the skills a device can run (no requirement, or any-of match).
// Empty deviceCaps is fail-open, except environment skills which always need the capability.
func Supported(deviceCaps map[string]bool) []string {
	out := make([]string, 0, len(Catalog))
	for _, name := range Catalog {
		if Disabled[name] {
			continue
		}
		if name == "environment" && !deviceCaps[device.CapEnvironment] {
			continue
		}
		reqs := Capability[name]
		if len(deviceCaps) == 0 || len(reqs) == 0 || hasAny(deviceCaps, reqs) {
			out = append(out, name)
		}
	}
	return out
}

// hasAny reports whether deviceCaps declares at least one of reqs.
func hasAny(deviceCaps map[string]bool, reqs []string) bool {
	for _, c := range reqs {
		if deviceCaps[c] {
			return true
		}
	}
	return false
}
