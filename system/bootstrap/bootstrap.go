package bootstrap

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/bootstrap/config"
	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/hal"
)

// semverRe captures the first semver-like token (e.g. 2026.3.8 or v1.2.3-beta).
var semverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// versionParts extracts the numeric dotted core of a version (1.2.3 -> [1 2 3]); nil if none.
func versionParts(v string) []int {
	core := semverRe.FindString(v)
	if core == "" {
		return nil
	}
	if i := strings.IndexAny(core, "-+_"); i >= 0 {
		core = core[:i]
	}
	var out []int
	for _, p := range strings.Split(core, ".") {
		n, err := strconv.Atoi(p)
		if err != nil {
			break
		}
		out = append(out, n)
	}
	return out
}

// compareVersions returns -1/0/1 comparing numeric cores; unparseable sorts lowest.
func compareVersions(a, b string) int {
	pa, pb := versionParts(a), versionParts(b)
	n := len(pa)
	if len(pb) > n {
		n = len(pb)
	}
	for i := 0; i < n; i++ {
		var x, y int
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		if x < y {
			return -1
		}
		if x > y {
			return 1
		}
	}
	return 0
}

// forceTargetAllowed lists components the force endpoints may target.
// componentInstalled still gates whether the work happens.
var forceTargetAllowed = map[string]bool{
	domain.OTAKeyOSServer: true, domain.OTAKeyBootstrap: true, domain.OTAKeyWeb: true, domain.OTAKeyHal: true,
	domain.OTAKeyDevice: true,
	domain.OTAKeyCodex:  true, domain.OTAKeyClaudeCode: true, domain.OTAKeyOpenCode: true, domain.OTAKeyPicoClaw: true,
	domain.OTAKeyHermes: true,
	// Must match os-server's ota.allowedTargets: the Agent update button on an
	// OpenClaw device arrives here as "openclaw".
	domain.OTAKeyOpenClaw: true,
}

// Bootstrap is the simplified OTA worker.
type Bootstrap struct {
	cfg    *config.Config
	client *http.Client
	state  *state.State
	// announcedThisCycle limits the "device is updating" cue to once per checkOnce.
	announcedThisCycle bool
	// security records the last metadata fetch outcome for GET /security.
	security securityTracker
	// rollbackConfigPath overrides the on-device config location in isolated tests.
	rollbackConfigPath string
	pendingUpdatePath  string
}

// configRetryInterval is how often Serve reloads bootstrap.json while unprovisioned.
const configRetryInterval = 30 * time.Second

// otaErrorLEDDisplayDuration is how long the failed-update cue stays visible.
const otaErrorLEDDisplayDuration = 10 * time.Second

// scheduleOTAErrorRestore is a test seam over time.AfterFunc.
var scheduleOTAErrorRestore = func(delay time.Duration, restore func()) {
	time.AfterFunc(delay, restore)
}

// ProvideServer creates a Bootstrap; the metadata URL may still be empty.
func ProvideServer() (*Bootstrap, error) {
	cfg := config.LoadOrDefault()
	st, err := state.Load(cfg.StateFile)
	if err != nil {
		return nil, fmt.Errorf("load state: %w", err)
	}
	return &Bootstrap{
		cfg:    cfg,
		client: &http.Client{Timeout: 20 * time.Second},
		state:  st,
	}, nil
}

// waitForConfig blocks until bootstrap.json yields a metadata URL; false on ctx cancel.
// Runs before other goroutines start, so reassigning b.cfg is race-free.
func (b *Bootstrap) waitForConfig(ctx context.Context) bool {
	for strings.TrimSpace(b.cfg.MetadataURL) == "" {
		slog.Warn("waiting for metadata_url in bootstrap config (device not provisioned yet)",
			"component", "bootstrap", "path", "/root/config/bootstrap.json")
		select {
		case <-ctx.Done():
			return false
		case <-time.After(configRetryInterval):
		}
		b.cfg = config.LoadOrDefault()
	}
	return true
}

// Serve runs the healthcheck HTTP server with OTA checks in the background.
func (b *Bootstrap) Serve() error {
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	if !b.waitForConfig(ctx) {
		return nil
	}

	pollInterval, err := time.ParseDuration(b.cfg.PollInterval)
	if err != nil {
		return fmt.Errorf("parse poll interval: %w", err)
	}
	slog.Info("bootstrap started", "component", "bootstrap", "metadataURL", b.cfg.MetadataURL, "interval", b.cfg.PollInterval)

	go b.checkLoop(ctx, pollInterval)

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.GET("/health", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	})
	r.GET("/security", func(c *gin.Context) {
		c.JSON(http.StatusOK, b.securityStatus())
	})
	// Cheap sibling of /versions (no metadata fetch) for UI polling during an update.
	r.GET("/updating", func(c *gin.Context) {
		c.JSON(http.StatusOK, gin.H{"updating": UpdatesInFlight()})
	})
	r.GET("/versions", func(c *gin.Context) {
		c.JSON(http.StatusOK, b.versionReport(c.Request.Context()))
	})
	r.POST("/force-check", func(c *gin.Context) {
		go func() {
			if err := b.checkOnce(context.Background()); err != nil {
				slog.Error("force check failed", "component", "bootstrap", "error", err)
			}
		}()
		c.JSON(http.StatusOK, gin.H{"status": "ok", "message": "update check triggered"})
	})
	// force-update installs the published version now, ignoring min_version.
	r.POST("/force-update/:target", func(c *gin.Context) {
		target := c.Param("target")
		if !forceTargetAllowed[target] {
			c.JSON(http.StatusBadRequest, gin.H{"error": "unknown target: " + target})
			return
		}
		// Async: an install outlives any HTTP timeout.
		go func() {
			if err := b.forceUpdate(context.Background(), target); err != nil {
				slog.Error("force update failed", "component", "bootstrap", "target", target, "error", err)
			}
		}()
		c.JSON(http.StatusOK, gin.H{"status": "ok", "message": "update started", "target": target})
	})
	r.POST("/force-check/:target", func(c *gin.Context) {
		target := c.Param("target")
		if !forceTargetAllowed[target] {
			c.JSON(http.StatusBadRequest, gin.H{"error": "unknown target: " + target})
			return
		}
		go func() {
			if err := b.checkComponent(context.Background(), target); err != nil {
				slog.Error("force check failed", "component", "bootstrap", "target", target, "error", err)
			}
		}()
		c.JSON(http.StatusOK, gin.H{"status": "ok", "message": "update check triggered", "target": target})
	})

	port := b.cfg.HttpPort
	srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port), Handler: r}
	go func() {
		<-ctx.Done()
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()
		_ = srv.Shutdown(shutdownCtx)
	}()
	slog.Info("healthcheck listening", "component", "bootstrap", "port", port)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		return fmt.Errorf("healthcheck server: %w", err)
	}
	return nil
}

// checkLoop runs OTA checks on a ticker.
func (b *Bootstrap) checkLoop(ctx context.Context, pollInterval time.Duration) {
	if err := b.checkOnce(ctx); err != nil {
		slog.Error("initial check failed", "component", "bootstrap", "error", err)
	}

	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := b.checkOnce(ctx); err != nil {
				slog.Error("check failed", "component", "bootstrap", "error", err)
			}
		}
	}
}

// checkComponent fetches metadata and reconciles a single named component.
func (b *Bootstrap) checkComponent(ctx context.Context, key string) error {
	if err := b.recoverPendingUpdate(ctx); err != nil {
		return err
	}
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		return err
	}
	component, ok := meta[key]
	if !ok {
		return fmt.Errorf("component %q not found in metadata", key)
	}
	updated, err := b.reconcile(ctx, key, component)
	if err != nil {
		return err
	}
	if updated {
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			return fmt.Errorf("save state: %w", err)
		}
	}
	return nil
}

// checkOnce fetches metadata and reconciles all components.
func (b *Bootstrap) checkOnce(ctx context.Context) error {
	if err := b.recoverPendingUpdate(ctx); err != nil {
		return err
	}
	meta, err := b.fetchMetadata(ctx)
	if err != nil {
		return err
	}
	if len(meta) == 0 {
		slog.Warn("empty metadata", "component", "bootstrap", "url", b.cfg.MetadataURL)
		return nil
	}

	// Update the on-device updater first: components below delegate installs to it.
	b.refreshUpdater(ctx)

	b.announcedThisCycle = false

	changed := false
	var devicePrerequisiteErr error
	// Agent-runtime CLIs are gated by componentInstalled; hermes also needs a commit pin.
	for _, key := range []string{
		domain.OTAKeyOSServer, domain.OTAKeyBootstrap, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy,
		domain.OTAKeyOpenClaw, domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw,
		domain.OTAKeyHermes,
	} {
		component, ok := meta[key]
		if !ok || !hermesPinned(key, component) {
			continue
		}
		updated, err := b.reconcile(ctx, key, component)
		if err != nil {
			slog.Error("reconcile error", "component", "bootstrap", "key", key, "error", err)
			if key == domain.OTAKeyHal || key == domain.OTAKeyOSServer {
				devicePrerequisiteErr = errors.Join(devicePrerequisiteErr, fmt.Errorf("%s update failed: %w", key, err))
			}
			continue
		}
		if updated {
			changed = true
		}
	}

	// A newer profile may require the HAL or OS schema from this release.
	// Preserve the current profile when either prerequisite failed to update.
	if devicePrerequisiteErr != nil {
		slog.Warn("device profile update skipped after prerequisite failure", "component", "bootstrap", "error", devicePrerequisiteErr)
	} else if updated, err := b.reconcileDevice(ctx); err != nil {
		slog.Error("device reconcile error", "component", "bootstrap", "error", err)
	} else if updated {
		changed = true
	}

	if changed {
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			return fmt.Errorf("save state: %w", err)
		}
	}
	return devicePrerequisiteErr
}

// resolveSTTLanguage returns the configured stt_language code, or "".
func resolveSTTLanguage() string {
	data, err := os.ReadFile("/root/config/config.json")
	if err != nil {
		return ""
	}
	var c struct {
		STTLanguage string `json:"stt_language"`
	}
	if json.Unmarshal(data, &c) != nil {
		return ""
	}
	return strings.TrimSpace(c.STTLanguage)
}

// otaUpdateStartPhrase returns the localized "device is updating" phrase (vi/zh/en).
func otaUpdateStartPhrase(lang string) string {
	switch {
	case strings.HasPrefix(lang, "vi"):
		return "Thiết bị đang cập nhật, sẽ mất một chút thời gian, vui lòng chờ trong khi cập nhật."
	case strings.HasPrefix(lang, "zh"):
		return "设备正在更新，需要一点时间,请稍候。"
	default:
		return "Device is updating. This will take a moment, please wait."
	}
}

// announceUpdateStart speaks the update cue once per cycle; errors are logged only.
// Skipped on devices without the audio capability.
func (b *Bootstrap) announceUpdateStart() {
	if b.announcedThisCycle {
		return
	}
	b.announcedThisCycle = true
	if !device.Has(resolveDeviceType(), device.CapAudio) {
		return
	}
	phrase := otaUpdateStartPhrase(resolveSTTLanguage())
	slog.Info("OTA update cue", "component", "bootstrap", "phrase", phrase)
	if err := hal.SpeakCached(phrase); err != nil {
		slog.Warn("OTA update cue speak failed", "component", "bootstrap", "error", err)
	}
}

// progressLED shows an OTA status preset by name; skipped on bodies without light.
func (b *Bootstrap) progressLED(state string) {
	if device.Has(resolveDeviceType(), device.CapLight) {
		hal.SetStatus(state)
	}
}

// restoreLED returns the strip to the user's LED state or the ambient resting look.
func (b *Bootstrap) restoreLED() {
	if device.Has(resolveDeviceType(), device.CapLight) {
		hal.RestoreLED()
	}
}

// showOTAErrorLED briefly pulses red, then restores the LED state.
func (b *Bootstrap) showOTAErrorLED() {
	b.progressLED("ota_error")
	scheduleOTAErrorRestore(otaErrorLEDDisplayDuration, b.restoreLED)
}

// resolveDeviceType returns DEVICE_TYPE env or config device_type; "" if unset (no fallback).
func resolveDeviceType() string {
	if t := strings.TrimSpace(os.Getenv("DEVICE_TYPE")); t != "" {
		return t
	}
	if data, err := os.ReadFile("/root/config/config.json"); err == nil {
		var c struct {
			DeviceType string `json:"device_type"`
		}
		if json.Unmarshal(data, &c) == nil && strings.TrimSpace(c.DeviceType) != "" {
			return strings.TrimSpace(c.DeviceType)
		}
	}
	return ""
}

// fetchDeviceComponent reads metadata.devices.<type>.
func (b *Bootstrap) fetchDeviceComponent(ctx context.Context, deviceType string) (domain.OTAComponent, bool, error) {
	payload, verified, err := b.fetchMetadataPayload(ctx)
	if err != nil {
		return domain.OTAComponent{}, false, err
	}
	var wrap struct {
		Devices map[string]domain.OTAComponent `json:"devices"`
	}
	if err := json.Unmarshal(payload, &wrap); err != nil {
		return domain.OTAComponent{}, false, fmt.Errorf("decode verified metadata: %w", err)
	}
	if verified {
		if err := validateOTAMetadata(domain.OTAMetadata(wrap.Devices)); err != nil {
			return domain.OTAComponent{}, false, err
		}
	}
	comp, ok := wrap.Devices[deviceType]
	return comp, ok, nil
}

// reconcileDevice updates this device's profile via `software-update device`.
func (b *Bootstrap) reconcileDevice(ctx context.Context) (bool, error) {
	deviceType := resolveDeviceType()
	if deviceType == "" {
		slog.Warn("device_type unresolved — skipping device-profile OTA (set DEVICE_TYPE; refusing to assume lamp)", "component", "bootstrap")
		return false, nil
	}
	comp, ok, err := b.fetchDeviceComponent(ctx, deviceType)
	if err != nil {
		return false, err
	}
	if !ok || strings.TrimSpace(comp.Version) == "" {
		return false, nil
	}
	return b.reconcile(ctx, domain.OTAKeyDevice, comp)
}

// reconcile decides whether the automatic worker updates a component.
// It only upgrades devices strictly below the floor (MinVersion, default Version);
// manual `software-update <key>` bypasses this and always installs Version.
func (b *Bootstrap) reconcile(ctx context.Context, key string, target domain.OTAComponent) (bool, error) {
	targetVersion := strings.TrimSpace(target.Version)
	if targetVersion == "" {
		return false, fmt.Errorf("metadata[%s].version is empty", key)
	}
	minVersion := strings.TrimSpace(target.MinVersion)
	if minVersion == "" {
		minVersion = targetVersion
	}
	blockedVersions, err := b.rollbackVersions()
	if err != nil {
		return false, err
	}
	if strings.TrimSpace(blockedVersions[key]) == targetVersion {
		slog.Warn("update blocked after local rollback", "component", "bootstrap", "key", key, "version", targetVersion)
		return false, nil
	}

	current := b.detectVersion(ctx, key)
	if current == "" {
		current = b.state.Components[key]
	}

	// Skip components absent from this device (detectVersion "" would loop forever);
	// a present binary with broken --version is still updated.
	if current == "" && !b.componentInstalled(key) {
		slog.Debug("component not installed on this device — skipping", "component", "bootstrap", "key", key)
		return false, nil
	}

	if compareVersions(current, minVersion) >= 0 {
		// A newer build is held back by the floor; log it for staged rollouts.
		if compareVersions(current, targetVersion) < 0 {
			slog.Info("update held by min_version floor", "component", "bootstrap", "key", key, "current", current, "min", minVersion, "target", targetVersion)
		}
		if current != "" && b.state.Components[key] != current {
			b.state.Components[key] = current
			return true, nil
		}
		return false, nil
	}

	slog.Info("update available", "component", "bootstrap", "key", key, "current", current, "min", minVersion, "target", targetVersion)

	// Speak before the LED + apply so speech does not race a HAL restart.
	b.announceUpdateStart()

	b.progressLED("ota_progress")

	if err := b.applyUpdate(ctx, key, target); err != nil {
		b.showOTAErrorLED()
		return false, err
	}

	// The success flash lasts ~750ms; wait 1s before restoring.
	b.progressLED("ota_success")
	time.Sleep(time.Second)
	b.restoreLED()
	// The updater replaces this process asynchronously: record the version only once
	// a later poll observes it, so a failed self-update is not persisted as success.
	if key == domain.OTAKeyBootstrap {
		slog.Info("bootstrap update staged; waiting for restarted version confirmation", "component", "bootstrap", "version", targetVersion)
		return false, nil
	}
	slog.Info("updated", "component", "bootstrap", "key", key, "version", targetVersion)
	b.state.Components[key] = targetVersion
	return true, nil
}

// fetchMetadata fetches OTA metadata JSON from the configured URL.
func (b *Bootstrap) fetchMetadata(ctx context.Context) (domain.OTAMetadata, error) {
	payload, verified, err := b.fetchMetadataPayload(ctx)
	if err != nil {
		return nil, err
	}
	return decodeOTAMetadataPayload(payload, verified)
}

func (b *Bootstrap) fetchMetadataPayload(ctx context.Context) (payload []byte, verified bool, err error) {
	// Record every outcome, including transport failures, in the security status.
	defer func() { b.security.record(verified, err) }()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, b.cfg.MetadataURL, nil)
	if err != nil {
		return nil, false, fmt.Errorf("build metadata request: %w", err)
	}
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, false, fmt.Errorf("fetch metadata %s: %w", b.cfg.MetadataURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false, fmt.Errorf("fetch metadata %s: status %s", b.cfg.MetadataURL, resp.Status)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return nil, false, fmt.Errorf("read metadata: %w", err)
	}
	if strings.TrimSpace(b.cfg.SigningPublicKey) == "" {
		slog.Warn("OTA signature verification disabled: signing_public_key is not provisioned", "component", "bootstrap")
		return data, false, nil
	}
	payload, err = verifyOTAMetadata(data, b.cfg.SigningPublicKey)
	if err != nil {
		return nil, false, fmt.Errorf("verify metadata %s: %w", b.cfg.MetadataURL, err)
	}
	return payload, true, nil
}

func decodeOTAMetadataPayload(payload []byte, requireChecksums bool) (domain.OTAMetadata, error) {
	var meta domain.OTAMetadata
	if err := json.Unmarshal(payload, &meta); err != nil {
		return nil, fmt.Errorf("decode verified metadata payload: %w", err)
	}
	if requireChecksums {
		if err := validateOTAMetadata(meta); err != nil {
			return nil, err
		}
	}
	return meta, nil
}

// detectVersion returns the current installed version for a component.
func (b *Bootstrap) detectVersion(ctx context.Context, key string) string {
	runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()

	switch key {
	case domain.OTAKeyOSServer:
		out, err := system.Run(runCtx, "os-server", "--version")
		if err != nil {
			return ""
		}
		return normalizeVersion(string(out))
	case domain.OTAKeyBootstrap:
		return strings.TrimSpace(config.BootstrapVersion)
	case domain.OTAKeyWeb:
		path := filepath.Join("/usr/share/nginx/html/setup", "VERSION")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyHal:
		path := filepath.Join("/opt/hal", "VERSION_HAL")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyBuddy:
		path := filepath.Join("/opt/claude-desktop-buddy", "VERSION_BUDDY")
		data, err := os.ReadFile(path)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyOpenClaw:
		out, err := system.Run(runCtx, "openclaw", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyCodex:
		out, err := system.Run(runCtx, "codex", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyClaudeCode:
		out, err := system.Run(runCtx, "claude", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyOpenCode:
		out, err := system.Run(runCtx, "opencode", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyHermes:
		out, err := system.Run(runCtx, "hermes", "--version")
		if err != nil {
			return ""
		}
		return cliSemver(string(out))
	case domain.OTAKeyPicoClaw:
		// Not `picoclaw version`: it prints a build description, not the release tag.
		data, err := os.ReadFile(domain.PicoClawVersionStamp)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	case domain.OTAKeyDevice:
		dir := os.Getenv("DEVICES_DIR")
		if dir == "" {
			dir = "/opt/devices"
		}
		data, err := os.ReadFile(filepath.Join(dir, resolveDeviceType(), "VERSION"))
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(data))
	default:
		return ""
	}
}

// componentInstalled reports whether the component exists on the device at all.
// Coarser than detectVersion so a present-but-unreadable install can be repaired.
// Keep in step with detectVersion and robots/<type>/software-update.
func (b *Bootstrap) componentInstalled(key string) bool {
	switch key {
	case domain.OTAKeyBootstrap:
		// Always, otherwise the worker could never self-update.
		return true
	case domain.OTAKeyOSServer:
		return inPath("os-server")
	case domain.OTAKeyOpenClaw:
		// Absent on devices running another runtime; binary presence is meaningful here.
		return inPath("openclaw")
	case domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw:
		// Every image bakes all agent CLIs, so gate on the configured runtime, and
		// on the on-device updater knowing the key (it is never updated over OTA).
		return resolveAgentRuntime() == key && updaterSupports(key)
	case domain.OTAKeyHermes:
		// The updater must also be the pinning one; the old one follows upstream HEAD.
		return resolveAgentRuntime() == key && updaterSupports(key) && updaterSupportsHermesPin()
	case domain.OTAKeyWeb:
		return dirExists("/usr/share/nginx/html/setup")
	case domain.OTAKeyHal:
		return dirExists("/opt/hal")
	case domain.OTAKeyBuddy:
		return dirExists("/opt/claude-desktop-buddy")
	case domain.OTAKeyDevice:
		dir := os.Getenv("DEVICES_DIR")
		if dir == "" {
			dir = "/opt/devices"
		}
		deviceType := resolveDeviceType()
		if deviceType == "" {
			return false
		}
		return dirExists(filepath.Join(dir, deviceType))
	default:
		return false
	}
}

// resolveAgentRuntime returns agent_runtime from config.json, or "" (skip CLI updates).
func resolveAgentRuntime() string {
	data, err := os.ReadFile("/root/config/config.json")
	if err != nil {
		return ""
	}
	var c struct {
		AgentRuntime string `json:"agent_runtime"`
	}
	if json.Unmarshal(data, &c) != nil {
		return ""
	}
	return strings.TrimSpace(c.AgentRuntime)
}

// updaterSupports reports whether software-update has a `[ "$APP" = "<key>" ]` branch.
// A missing script means no support.
func updaterSupports(key string) bool {
	path, err := exec.LookPath("software-update")
	if err != nil {
		return false
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return strings.Contains(string(data), `[ "$APP" = "`+key+`" ]`)
}

// updaterSupportsHermesPin reports whether software-update reads `.hermes.commit`.
func updaterSupportsHermesPin() bool {
	path, err := exec.LookPath("software-update")
	if err != nil {
		return false
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return strings.Contains(string(data), ".hermes.commit")
}

// hermesPinned rejects an unpinned hermes entry; everything else passes.
func hermesPinned(key string, component domain.OTAComponent) bool {
	return key != domain.OTAKeyHermes || strings.TrimSpace(component.Commit) != ""
}

func inPath(name string) bool {
	_, err := exec.LookPath(name)
	return err == nil
}

func dirExists(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

// applyUpdate runs the appropriate update command for the given component.
func (b *Bootstrap) applyUpdate(ctx context.Context, key string, component domain.OTAComponent) error {
	switch key {
	case domain.OTAKeyOSServer, domain.OTAKeyWeb, domain.OTAKeyHal, domain.OTAKeyBuddy, domain.OTAKeyOpenClaw, domain.OTAKeyDevice,
		domain.OTAKeyCodex, domain.OTAKeyClaudeCode, domain.OTAKeyOpenCode, domain.OTAKeyPicoClaw, domain.OTAKeyHermes:
		// Non-bootstrap components delegate to the on-device `software-update <key>`.
		runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
		defer cancel()
		out, err := system.Run(runCtx, "software-update", key)
		if err != nil {
			return fmt.Errorf("software-update %s: %w", key, err)
		}
		slog.Info("update output", "component", "bootstrap", "key", key, "output", out)
		return nil

	case domain.OTAKeyBootstrap:
		// Detached so it survives bootstrap exit.
		slog.Info("spawning background software-update bootstrap", "component", "bootstrap")
		if err := system.SpawnBackground("software-update", "bootstrap"); err != nil {
			return fmt.Errorf("spawn software-update bootstrap: %w", err)
		}
		return nil

	default:
		return fmt.Errorf("unsupported component %q", key)
	}
}

// cliSemver extracts the semver from the first line of an agent CLI's --version.
// Example: "codex-cli 0.142.5" -> "0.142.5". Not usable for picoclaw.
func cliSemver(raw string) string {
	line := strings.TrimSpace(strings.TrimRight(raw, "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := semverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1]
	}
	return ""
}

// normalizeVersion extracts a semver-like version, e.g. "os-server 1.0.83" -> "1.0.83".
func normalizeVersion(raw string) string {
	line := strings.TrimSpace(strings.TrimRight(raw, "\r\n"))
	if line == "" {
		return ""
	}
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := semverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1]
	}
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return ""
	}
	return strings.TrimSpace(fields[len(fields)-1])
}
