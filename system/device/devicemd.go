package device

import (
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var (
	reFrontMatter     = regexp.MustCompile(`(?s)^---\s*\n(.*?)\n---\s*\n`)
	reGatewayBlock    = regexp.MustCompile(`(?m)^gateway:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reGatewayDefault  = regexp.MustCompile(`(?m)^[ \t]+default:[ \t]*(\S+)`)
	reGatewayProtocol = regexp.MustCompile(`(?m)^[ \t]+protocol:[ \t]*(\S+)`)
	reVoiceBlock      = regexp.MustCompile(`(?m)^voice:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reVoiceProvider   = regexp.MustCompile(`(?m)^[ \t]+tts_provider:[ \t]*(\S+)`)
	reVoiceVoice      = regexp.MustCompile(`(?m)^[ \t]+tts_voice:[ \t]*(\S+)`)
	reVoiceWakeWord   = regexp.MustCompile(`(?m)^[ \t]+wakeword:[ \t]*(\S+)`)
	reSoulRef         = regexp.MustCompile(`(?m)^soul_ref:[ \t]*(\S+)`)
	reCapBlock        = regexp.MustCompile(`(?m)^capabilities:[ \t]*\n((?:[ \t]+.*\n?)+)`)
	reCapKey          = regexp.MustCompile(`(?m)^[ \t]+(\w+):`)
	reStartupVolume   = regexp.MustCompile(`(?m)^startup_volume:[ \t]*(\d+)`)
)

// DefaultStartupVolume is the startup speaker volume when ROBOT.md declares none.
const DefaultStartupVolume = 100

// readFrontMatter returns the front-matter of robots/<deviceType>/ROBOT.md, or nil.
func readFrontMatter(deviceType string) []byte {
	// DEVICE.md is the legacy name still on devices in the field.
	dir := filepath.Join(DevicesDir(), deviceType)
	b, err := os.ReadFile(filepath.Join(dir, "ROBOT.md"))
	if err != nil {
		b, err = os.ReadFile(filepath.Join(dir, "DEVICE.md"))
	}
	if err != nil {
		return nil
	}
	fm := reFrontMatter.FindSubmatch(b)
	if fm == nil {
		return nil
	}
	return fm[1]
}

// Capabilities returns the capability keys declared in ROBOT.md, or nil if absent.
func Capabilities(deviceType string) map[string]bool {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return nil
	}
	blk := reCapBlock.FindSubmatch(fm)
	if blk == nil {
		return nil
	}
	caps := map[string]bool{}
	for _, m := range reCapKey.FindAllSubmatch(blk[1], -1) {
		caps[strings.TrimSpace(string(m[1]))] = true
	}
	return caps
}

// CapEnvironment identifies model-independent environmental acquisition.
const CapEnvironment = "environment"

// Capability names (capabilities.v1). Keep in sync with
// robots/contract/capabilities.md and skills.Capability / skills.HookCapability.
const (
	CapAudio    = "audio"
	CapVision   = "vision"
	CapSensing  = "sensing"
	CapPresence = "presence"
	CapMotion   = "motion"
	CapPolicy   = "policy"
	CapLight    = "light"
	CapDisplay  = "display"
	// CapExpression means the body can show emotion (the /emotion route).
	CapExpression = "expression"
	// CapLifelike opts into system/ambient idle behaviors (routeless).
	CapLifelike     = "lifelike"
	CapMedia        = "media"
	CapConnectivity = "connectivity"
	CapCompanion    = "companion"
	CapSystem       = "system"
)

// RouteCapability maps a HAL route path to its required capability, or "" when
// ungated (unmapped paths fail open).
// Example: RouteCapability("/servo/move") == CapMotion
func RouteCapability(path string) string {
	switch {
	case strings.HasPrefix(path, "/emotion"):
		return CapExpression
	case strings.HasPrefix(path, "/scene"), strings.HasPrefix(path, "/led"):
		return CapLight
	case strings.HasPrefix(path, "/servo"):
		return CapMotion
	case strings.HasPrefix(path, "/policy"):
		return CapPolicy
	case strings.HasPrefix(path, "/display"):
		return CapDisplay
	case strings.HasPrefix(path, "/music"):
		return CapMedia
	default:
		return ""
	}
}

// Has reports whether deviceType declares capability; true when no
// capabilities block is readable (fail-open).
func Has(deviceType, capability string) bool {
	caps := Capabilities(deviceType)
	if len(caps) == 0 {
		return true
	}
	return caps[capability]
}

// SoulRef returns the `soul_ref` from ROBOT.md (path or http(s) URL), or "".
func SoulRef(deviceType string) string {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return ""
	}
	m := reSoulRef.FindSubmatch(fm)
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// StartupVolume returns `startup_volume` (0-100) from ROBOT.md, else
// DefaultStartupVolume; never falls back to silent.
func StartupVolume(deviceType string) int {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return DefaultStartupVolume
	}
	m := reStartupVolume.FindSubmatch(fm)
	if m == nil {
		return DefaultStartupVolume
	}
	v, err := strconv.Atoi(strings.TrimSpace(string(m[1])))
	if err != nil || v < 0 || v > 100 {
		return DefaultStartupVolume
	}
	return v
}

// DevicesDir resolves the per-device profile root (robots/<type>/...).
// DEVICES_DIR env wins; falls back to /opt/devices (mirrors HAL + onboarding.go).
func DevicesDir() string {
	if d := os.Getenv("DEVICES_DIR"); d != "" {
		return d
	}
	return "/opt/devices"
}

// gatewayField extracts one sub-field (matched by re) from the `gateway:` block
// of robots/<deviceType>/ROBOT.md, or "" if absent/unreadable.
func gatewayField(deviceType string, re *regexp.Regexp) string {
	return blockField(deviceType, reGatewayBlock, re)
}

// blockField extracts fieldRe from the blockRe front-matter block, or "".
func blockField(deviceType string, blockRe, fieldRe *regexp.Regexp) string {
	fm := readFrontMatter(deviceType)
	if fm == nil {
		return ""
	}
	blk := blockRe.FindSubmatch(fm)
	if blk == nil {
		return ""
	}
	m := fieldRe.FindSubmatch(blk[1])
	if m == nil {
		return ""
	}
	return strings.TrimSpace(string(m[1]))
}

// GatewayDefault returns the `gateway.default` (agentic runtime) declared in
// robots/<deviceType>/ROBOT.md, or "" if absent.
func GatewayDefault(deviceType string) string {
	return gatewayField(deviceType, reGatewayDefault)
}

// GatewayProtocol returns `gateway.protocol` from ROBOT.md, or "" (consistency
// guard only; transport is a runtime property).
func GatewayProtocol(deviceType string) string {
	return gatewayField(deviceType, reGatewayProtocol)
}

// voiceField extracts one sub-field (matched by re) from the `voice:` block of
// robots/<deviceType>/ROBOT.md, or "" if absent/unreadable.
func voiceField(deviceType string, re *regexp.Regexp) string {
	return blockField(deviceType, reVoiceBlock, re)
}

// TTSProvider returns the device's default `voice.tts_provider` from ROBOT.md, or "".
func TTSProvider(deviceType string) string {
	return voiceField(deviceType, reVoiceProvider)
}

// TTSVoice returns the device's default `voice.tts_voice` from ROBOT.md, or "".
func TTSVoice(deviceType string) string {
	return voiceField(deviceType, reVoiceVoice)
}

// WakeWordDefault returns the out-of-the-box `voice.wakeword` from ROBOT.md and
// whether it was declared; only adopted when config.json has no wakeword key.
func WakeWordDefault(deviceType string) (value bool, declared bool) {
	switch strings.ToLower(voiceField(deviceType, reVoiceWakeWord)) {
	case "true":
		return true, true
	case "false":
		return false, true
	default:
		return false, false
	}
}
