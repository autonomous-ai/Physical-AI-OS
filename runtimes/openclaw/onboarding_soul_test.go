package openclaw

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// repoDevicesDir walks up from the test dir to find the committed robots/ tree.
func repoDevicesDir(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("getwd: %v", err)
	}
	for dir := wd; dir != filepath.Dir(dir); dir = filepath.Dir(dir) {
		candidate := filepath.Join(dir, "robots")
		if st, err := os.Stat(filepath.Join(candidate, "lamp")); err == nil && st.IsDir() {
			return candidate
		}
	}
	t.Fatalf("robots/ tree not found above %s", wd)
	return ""
}

func soulFor(t *testing.T, deviceType string) ([]byte, bool) {
	t.Helper()
	t.Setenv("DEVICES_DIR", repoDevicesDir(t))
	t.Setenv("DEVICE_TYPE", deviceType)
	s := &OpenclawService{config: &config.Config{}}
	content, has, err := s.deviceSoulCore()
	if err != nil {
		t.Fatalf("deviceSoulCore(%q): %v", deviceType, err)
	}
	return content, has
}

// Lamp ships its own persona → we override the gateway default with it.
func TestDeviceSoulCore_LampHasOwnSoul(t *testing.T) {
	content, has := soulFor(t, "lamp")
	if !has {
		t.Fatal("lamp must resolve a SOUL.md")
	}
	if !strings.Contains(string(content), "You are **Lamp**") {
		t.Errorf("lamp soul missing persona text; got start %q", head(content))
	}
}

// Intern-v2 ships its own persona, independent of Lamp's.
func TestDeviceSoulCore_InternHasOwnSoul(t *testing.T) {
	content, has := soulFor(t, "intern-v2")
	if !has {
		t.Fatal("intern-v2 must resolve a SOUL.md")
	}
	if !strings.Contains(string(content), "You are **Intern**") {
		t.Errorf("intern soul missing persona text; got start %q", head(content))
	}
}

// A body without soul_ref leaves the agentic runtime's default soul intact.
func TestDeviceSoulCore_NoSoulRef(t *testing.T) {
	devicesDir := t.TempDir()
	deviceDir := filepath.Join(devicesDir, "no-persona")
	if err := os.MkdirAll(deviceDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(deviceDir, "ROBOT.md"), []byte("---\nname: no-persona\n---\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Even a file named SOUL.md must not be injected without an explicit ref.
	if err := os.WriteFile(filepath.Join(deviceDir, "SOUL.md"), []byte("Unreferenced persona"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DEVICES_DIR", devicesDir)
	t.Setenv("DEVICE_TYPE", "no-persona")
	s := &OpenclawService{config: &config.Config{}}
	content, has, err := s.deviceSoulCore()
	if err != nil {
		t.Fatalf("deviceSoulCore: %v", err)
	}
	if has || len(content) != 0 {
		t.Errorf("body without soul_ref must return no soul; got hasSoul=%v content=%q", has, content)
	}
}

// A different body (dog) gets its own soul from the same binary.
func TestDeviceSoulCore_DogHasOwnSoul(t *testing.T) {
	content, has := soulFor(t, "unitree-go2w")
	if !has {
		t.Fatal("unitree-go2w must resolve a SOUL.md")
	}
	if !strings.Contains(string(content), "Unitree Go2-W") {
		t.Errorf("dog soul missing its persona; got start %q", head(content))
	}
}

// Empty device_type no longer falls back to "lamp" — DeviceTypeOrDefault returns "" and resolves no soul (the Serve startup guard fail-louds instead).
func TestDeviceSoulCore_EmptyTypeNoLampFallback(t *testing.T) {
	if _, has := soulFor(t, ""); has {
		t.Error("empty device_type must NOT resolve the lamp soul (no fallback)")
	}
}

// An unknown device type has no profile on disk → no soul, no override.
func TestDeviceSoulCore_UnknownTypeHasNoSoul(t *testing.T) {
	if _, has := soulFor(t, "does-not-exist"); has {
		t.Error("unknown device type must return hasSoul=false (no embedded fallback)")
	}
}

// openclawDefaultSoul is the gateway's own default soul that OpenClaw seeds into workspace/SOUL.md on first boot.
const openclawDefaultSoul = `# SOUL.md - Who You Are

_You're not a chatbot. You're becoming someone._

## Core Truths

**Have opinions.** You're allowed to disagree.

## Related

- [SOUL.md personality guide](/concepts/soul)
`

// soulService builds a OpenclawService whose OpenclawConfigDir is an isolated temp dir and whose device soul resolves from the committed robots/ tree.
func soulService(t *testing.T, deviceType string) (*OpenclawService, string) {
	t.Helper()
	t.Setenv("DEVICES_DIR", repoDevicesDir(t))
	t.Setenv("DEVICE_TYPE", deviceType)
	cfgDir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(cfgDir, "workspace"), 0o755); err != nil {
		t.Fatalf("mkdir workspace: %v", err)
	}
	return &OpenclawService{config: &config.Config{OpenclawConfigDir: cfgDir, DeviceType: deviceType}}, cfgDir
}

func readSoul(t *testing.T, cfgDir string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(cfgDir, "workspace", "SOUL.md"))
	if err != nil {
		t.Fatalf("read SOUL.md: %v", err)
	}
	return string(b)
}

// When the OpenClaw gateway has seeded its own default soul, ensureSoulMDBlock must replace it with the device block — NOT keep it below `---` (the duplication bug).
func TestEnsureSoulMDBlock_StripsOpenClawDefault(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(openclawDefaultSoul), 0o644); err != nil {
		t.Fatalf("seed default soul: %v", err)
	}

	if _, err := s.ensureSoulMDBlock(); err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}

	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("openclaw default soul not stripped — duplication bug present:\n%s", got)
	}
	if !strings.Contains(got, "You are **Lamp**") {
		t.Errorf("device (lamp) soul missing after injection:\n%s", got)
	}
	if strings.Count(got, osMandatoryMarker) != 1 {
		t.Errorf("expected exactly one managed block, got %d markers", strings.Count(got, osMandatoryMarker))
	}
}

// Already-dup'd file (device block + openclaw default below `---`) must self-heal: the fast path has to fall through and strip the lingering default.
func TestEnsureSoulMDBlock_HealsExistingDuplicate(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	core, has, err := s.deviceSoulCore()
	if err != nil || !has {
		t.Fatalf("deviceSoulCore: has=%v err=%v", has, err)
	}
	block := osMandatoryMarker + "\n" + strings.TrimSpace(string(core)) + "\n---"
	dup := block + "\n\n" + openclawDefaultSoul
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(dup), 0o644); err != nil {
		t.Fatalf("seed dup: %v", err)
	}

	changed, err := s.ensureSoulMDBlock()
	if err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}
	if !changed {
		t.Fatal("expected ensureSoulMDBlock to heal the duplicate (changed=true)")
	}
	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("duplicate not healed:\n%s", got)
	}

	changed2, err := s.ensureSoulMDBlock()
	if err != nil {
		t.Fatalf("ensureSoulMDBlock (2nd): %v", err)
	}
	if changed2 {
		t.Errorf("second run rewrote a clean file — churn:\n%s", readSoul(t, cfgDir))
	}
}

// An owner `## Personal` section below the default must be preserved while the default soul above it is discarded.
func TestEnsureSoulMDBlock_PreservesOwnerPersonal(t *testing.T) {
	s, cfgDir := soulService(t, "lamp")
	const ownerNote = "My owner likes tea at 9pm."
	seed := openclawDefaultSoul + "\n## Personal\n\n" + ownerNote + "\n"
	soulPath := filepath.Join(cfgDir, "workspace", "SOUL.md")
	if err := os.WriteFile(soulPath, []byte(seed), 0o644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	if _, err := s.ensureSoulMDBlock(); err != nil {
		t.Fatalf("ensureSoulMDBlock: %v", err)
	}
	got := readSoul(t, cfgDir)
	if strings.Contains(got, "# SOUL.md - Who You Are") {
		t.Errorf("default soul above ## Personal not stripped:\n%s", got)
	}
	if !strings.Contains(got, ownerNote) {
		t.Errorf("owner ## Personal content lost:\n%s", got)
	}
}

func head(b []byte) string {
	const n = 48
	if len(b) < n {
		return string(b)
	}
	return string(b[:n])
}
