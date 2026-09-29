package bootstrap

import (
	"context"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
)

// ComponentVersion is one row of GET /versions.
type ComponentVersion struct {
	Current    string `json:"current"`
	Target     string `json:"target"`
	MinVersion string `json:"min_version"`
	// UpdateAvailable is true when the feed offers a newer build, regardless of the floor.
	UpdateAvailable bool `json:"update_available"`
	// HeldByFloor is true when a newer build exists but min_version holds it back.
	HeldByFloor bool `json:"held_by_floor"`
}

// versionReport lists installed components with their current and published versions.
func (b *Bootstrap) versionReport(ctx context.Context) map[string]ComponentVersion {
	out := map[string]ComponentVersion{}
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		slog.Warn("version report: metadata fetch failed", "component", "bootstrap", "error", err)
		return out
	}
	for _, key := range []string{
		domain.OTAKeyOSServer, domain.OTAKeyBootstrap, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy,
		domain.OTAKeyOpenClaw, domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw,
		domain.OTAKeyHermes,
	} {
		component, ok := meta[key]
		if !ok || !hermesPinned(key, component) || !b.componentInstalled(key) {
			continue
		}
		current := b.detectVersion(ctx, key)
		if current == "" {
			current = b.state.Components[key]
		}
		target := strings.TrimSpace(component.Version)
		minVersion := strings.TrimSpace(component.MinVersion)
		if minVersion == "" {
			minVersion = target
		}
		newer := compareVersions(current, target) < 0
		out[key] = ComponentVersion{
			Current:         current,
			Target:          target,
			MinVersion:      minVersion,
			UpdateAvailable: newer,
			HeldByFloor:     newer && compareVersions(current, minVersion) >= 0,
		}
	}
	// Device profiles are nested under metadata.devices.<type>; report them as "device".
	if deviceType := resolveDeviceType(); deviceType != "" && b.componentInstalled(domain.OTAKeyDevice) {
		component, ok, err := b.fetchDeviceComponent(ctx, deviceType)
		if err != nil {
			slog.Warn("version report: device metadata fetch failed", "component", "bootstrap", "device_type", deviceType, "error", err)
		} else if ok {
			current := b.detectVersion(ctx, domain.OTAKeyDevice)
			if current == "" {
				current = b.state.Components[domain.OTAKeyDevice]
			}
			target := strings.TrimSpace(component.Version)
			minVersion := strings.TrimSpace(component.MinVersion)
			if minVersion == "" {
				minVersion = target
			}
			newer := compareVersions(current, target) < 0
			out[domain.OTAKeyDevice] = ComponentVersion{
				Current: current, Target: target, MinVersion: minVersion,
				UpdateAvailable: newer,
				HeldByFloor:     newer && compareVersions(current, minVersion) >= 0,
			}
		}
	}
	return out
}
