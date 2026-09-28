package domain

// OTAComponent describes version and download URL for a single component.
// Auto-update triggers only below MinVersion (empty = Version); manual software-update always installs Version.
type OTAComponent struct {
	Version    string `json:"version"`
	MinVersion string `json:"min_version,omitempty"`
	URL        string `json:"url"`
	SHA256     string `json:"sha256,omitempty"`
	// Commit pins a git-installed component (hermes) to an upstream commit; empty = unpinned (HEAD).
	Commit string `json:"commit,omitempty"`
}

const (
	OTAKeyOSServer  = "os-server"
	OTAKeyBootstrap = "bootstrap"
	OTAKeyOpenClaw  = "openclaw"
	OTAKeyWeb       = "web"
	OTAKeyHal       = "hal"
	OTAKeyBuddy     = "claude-desktop-buddy"
	// OTAKeyDevice is not a flat metadata key; the profile lives at metadata.devices.<device_type>.
	OTAKeyDevice = "device"

	// Agent-runtime CLI keys must equal the config.json `agent_runtime` name; bootstrap updates only the active one.
	// Hermes auto-updates only when its entry carries a Commit (unpinned min_version is unreachable).
	OTAKeyHermes     = "hermes"
	OTAKeyCodex      = "codex"
	OTAKeyClaudeCode = "claudecode"
	OTAKeyOpenCode   = "opencode"
	// OTAKeyPicoClaw's version is a release tag, read from PicoClawVersionStamp.
	OTAKeyPicoClaw = "picoclaw"
)

// PicoClawVersionStamp records the installed PicoClaw release tag (the binary cannot report it).
const PicoClawVersionStamp = "/usr/local/lib/os-runtimes/picoclaw/installed-version"

// OTAMetadata is the JSON shape returned by the OTA metadata URL, keyed by component.
type OTAMetadata map[string]OTAComponent
