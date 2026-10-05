package bootstrap

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

func TestDecodeOTAMetadataRequiresValidSignatureAndChecksum(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"os-server":{"version":"1.2.3","url":"https://example.test/os.zip","sha256":"039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81"}}`)
	var envelope signedOTAMetadata
	envelope.Format = otaMetadataFormat
	envelope.Payload = base64.StdEncoding.EncodeToString(payload)
	envelope.Signature.Algorithm = "ed25519"
	envelope.Signature.Value = base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload))
	encoded, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}

	metadata, err := decodeOTAMetadata(encoded, base64.StdEncoding.EncodeToString(publicKey))
	if err != nil {
		t.Fatalf("decode signed metadata: %v", err)
	}
	if metadata[domain.OTAKeyOSServer].Version != "1.2.3" {
		t.Fatalf("decoded wrong metadata: %+v", metadata)
	}
	var legacyCompatible map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &legacyCompatible); err != nil {
		t.Fatal(err)
	}
	legacyCompatible["signed"] = encoded
	hybrid, err := json.Marshal(legacyCompatible)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeOTAMetadata(hybrid, base64.StdEncoding.EncodeToString(publicKey)); err != nil {
		t.Fatalf("hybrid metadata rejected: %v", err)
	}

	envelope.Payload = base64.StdEncoding.EncodeToString([]byte(`{"os-server":{"version":"9.9.9"}}`))
	tampered, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := decodeOTAMetadata(tampered, base64.StdEncoding.EncodeToString(publicKey)); err == nil {
		t.Fatal("tampered metadata was accepted")
	}
}

func TestVerifyArtifactSHA256(t *testing.T) {
	data := []byte("abc")
	if err := verifyArtifactSHA256(data, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"); err != nil {
		t.Fatalf("valid checksum rejected: %v", err)
	}
	if err := verifyArtifactSHA256(data, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"); err == nil {
		t.Fatal("wrong checksum was accepted")
	}
}

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.2.3", "1.2.3", 0},
		{"1.2.3", "1.2.4", -1},
		{"1.3.0", "1.2.9", 1},
		{"2.0.0", "1.9.9", 1},
		// numeric, not lexical: 27 > 9
		{"2026.5.27", "2026.5.9", 1},
		{"2026.5.9", "2026.5.27", -1},
		// pre-release/build suffix ignored (numeric core only)
		{"1.2.3-rc1", "1.2.3", 0},
		{"1.2.3+build5", "1.2.3", 0},
		// "v" prefix / surrounding text tolerated via semverRe extraction
		{"v1.4.0", "1.4.0", 0},
		// empty / unparseable sorts lowest
		{"", "0.0.1", -1},
		{"", "", 0},
		{"garbage", "1.0.0", -1},
	}
	for _, c := range cases {
		if got := compareVersions(c.a, c.b); got != c.want {
			t.Errorf("compareVersions(%q, %q) = %d, want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestForceTargetAllowedIncludesDebugVersionTargets(t *testing.T) {
	if !forceTargetAllowed[domain.OTAKeyBootstrap] {
		t.Fatal("bootstrap must be a force-update target so the debug Versions card can trigger its self-update")
	}
	if !forceTargetAllowed[domain.OTAKeyDevice] {
		t.Fatal("device must be a force-update target so the debug Versions card can update the installed profile")
	}
	if !forceTargetAllowed[domain.OTAKeyOpenClaw] {
		t.Fatal("openclaw must be a force-update target so the Agent update button works on OpenClaw devices")
	}
}

func TestOTAErrorLEDSchedulesRestore(t *testing.T) {
	devicesDir := t.TempDir()
	if err := os.Mkdir(filepath.Join(devicesDir, "no-light"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(devicesDir, "no-light", "ROBOT.md"), []byte("---\ncapabilities:\n  audio: {}\n---\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "no-light")

	original := scheduleOTAErrorRestore
	defer func() { scheduleOTAErrorRestore = original }()
	var gotDelay time.Duration
	var gotRestore func()
	scheduleOTAErrorRestore = func(delay time.Duration, restore func()) {
		gotDelay = delay
		gotRestore = restore
	}

	(&Bootstrap{}).showOTAErrorLED()

	if gotDelay != otaErrorLEDDisplayDuration {
		t.Fatalf("error LED restore delay = %v, want %v", gotDelay, otaErrorLEDDisplayDuration)
	}
	if gotRestore == nil {
		t.Fatal("error LED did not schedule a restore")
	}
	gotRestore()
}

// An absent component must not read as out of date.
func TestReconcileSkipsUninstalledComponent(t *testing.T) {
	devicesDir := t.TempDir() // no <type> subdir → the profile is not installed
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")

	b := &Bootstrap{
		state:              &state.State{Components: map[string]string{}},
		rollbackConfigPath: filepath.Join(t.TempDir(), "bootstrap.json"),
	}

	// A nil error proves the skip (applyUpdate would fail here).
	updated, err := b.reconcile(context.Background(), domain.OTAKeyDevice,
		domain.OTAComponent{Version: "9.9.9"})
	if err != nil {
		t.Fatalf("reconcile errored on a component this device does not have: %v", err)
	}
	if updated {
		t.Fatal("reconcile reported an update for a component this device does not have")
	}
	if v, ok := b.state.Components[domain.OTAKeyDevice]; ok {
		t.Fatalf("a skipped component must not be written to state, got %q", v)
	}
}

func TestReconcileSkipsVersionBlockedByRollback(t *testing.T) {
	b := &Bootstrap{
		cfg:                &config.Config{RollbackVersions: map[string]string{domain.OTAKeyOSServer: "1.2.3"}},
		state:              &state.State{Components: map[string]string{}},
		rollbackConfigPath: filepath.Join(t.TempDir(), "bootstrap.json"),
	}
	updated, err := b.reconcile(context.Background(), domain.OTAKeyOSServer, domain.OTAComponent{
		Version:    "1.2.3",
		MinVersion: "1.2.3",
	})
	if err != nil {
		t.Fatalf("reconcile blocked version: %v", err)
	}
	if updated {
		t.Fatal("reconcile updated a version blocked by rollback")
	}
}

func TestComponentInstalled(t *testing.T) {
	devicesDir := t.TempDir()
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "reachy-mini")
	b := &Bootstrap{}

	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with no profile directory")
	}
	if err := os.Mkdir(filepath.Join(devicesDir, "reachy-mini"), 0o755); err != nil {
		t.Fatal(err)
	}
	if !b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported missing although its directory exists")
	}
	if !b.componentInstalled(domain.OTAKeyBootstrap) {
		t.Error("bootstrap must always count as installed")
	}
	t.Setenv("DEVICE_TYPE", "")
	if b.componentInstalled(domain.OTAKeyDevice) {
		t.Error("device profile reported installed with an unresolved device type")
	}
}

func TestCLISemver(t *testing.T) {
	cases := map[string]string{
		"OpenClaw 2026.3.8 (3caab92)":  "2026.3.8",
		"codex-cli 0.142.5":            "0.142.5",
		"2.1.218 (Claude Code)":        "2.1.218",
		"1.18.4":                       "1.18.4",
		"0.5.2\nextra line 9.9.9":      "0.5.2",
		"nightly-44-g1959045c-dirty":   "",
		"picoclaw nightly-44-g1959045": "",
	}
	for raw, want := range cases {
		if got := cliSemver(raw); got != want {
			t.Errorf("cliSemver(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestComponentInstalledAgentCLIsFollowRuntime(t *testing.T) {
	// With no resolvable config, every agent CLI must report not installed.
	if _, err := os.Stat("/root/config/config.json"); err == nil {
		t.Skip("host has a real /root/config/config.json; runtime gate not isolatable")
	}
	b := &Bootstrap{}
	for _, key := range []string{domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw} {
		if b.componentInstalled(key) {
			t.Errorf("%s reported installed with an unresolvable agent_runtime", key)
		}
	}
}

func TestUpdaterSupports(t *testing.T) {
	dir := t.TempDir()
	script := `#!/bin/bash
# Usage: software-update <os-server|codex|picoclaw>   <- usage text alone must NOT count
if [ "$APP" = "os-server" ]; then
  :
elif [ "$APP" = "codex" ]; then
  :
fi
`
	if err := os.WriteFile(filepath.Join(dir, "software-update"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)

	if !updaterSupports(domain.OTAKeyCodex) {
		t.Error("codex branch present but reported unsupported")
	}
	// picoclaw appears only in the usage line; that must not count as support.
	if updaterSupports(domain.OTAKeyPicoClaw) {
		t.Error("picoclaw reported supported from its usage-string mention alone")
	}
	t.Setenv("PATH", t.TempDir())
	if updaterSupports(domain.OTAKeyCodex) {
		t.Error("missing software-update reported as supporting codex")
	}
}

// Hermes needs a pinning updater that reads .hermes.commit.
func TestUpdaterSupportsHermesPinRequiresCommitField(t *testing.T) {
	dir := t.TempDir()
	write := func(script string) {
		if err := os.WriteFile(filepath.Join(dir, "software-update"), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
	write("#!/bin/bash\nif [ \"$APP\" = \"hermes\" ]; then hermes update; fi\n")
	if !updaterSupports(domain.OTAKeyHermes) || updaterSupportsHermesPin() {
		t.Fatal("HEAD-following hermes branch must count as branch-present but NOT pin-capable")
	}
	write("#!/bin/bash\nif [ \"$APP\" = \"hermes\" ]; then HERMES_COMMIT=$(jq -r '.hermes.commit // empty' \"$METADATA_PAYLOAD\"); fi\n")
	if !updaterSupportsHermesPin() {
		t.Fatal("pinning updater not recognised")
	}
}

// An unpinned hermes entry is neither applied nor advertised.
func TestHermesPinnedGate(t *testing.T) {
	if hermesPinned(domain.OTAKeyHermes, domain.OTAComponent{Version: "0.21.1"}) {
		t.Fatal("unpinned hermes entry passed the gate")
	}
	if !hermesPinned(domain.OTAKeyHermes, domain.OTAComponent{Version: "0.21.1", Commit: "2237be355906fbe6065ce1815711eee52b2d646e"}) {
		t.Fatal("pinned hermes entry blocked")
	}
	if !hermesPinned(domain.OTAKeyCodex, domain.OTAComponent{Version: "0.142.5"}) {
		t.Fatal("non-hermes component blocked by the hermes gate")
	}
}
