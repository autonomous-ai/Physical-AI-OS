package system

import (
	"fmt"
	"log"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/osreset"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/server/serializers"
)

var deviceWipePaths = []string{
	"/root/config/config.json",                      // os-server config; bootstrap.json in the same dir is intentionally kept
	"/root/config/agent_state.json",                 // MUST wipe with config.json, or a spurious persona migration runs on next boot
	"/root/local/users",                             // face + voice enrollments (owner)
	"/root/local/strangers",                         // face + voice enrollments (stranger)
	"/var/lib/hal/snapshots",                        // persistent camera snapshots (sensing_face / motion / emotion, 72h TTL)
	"/etc/wpa_supplicant/wpa_supplicant-wlan0.conf", // home WiFi credentials → forces AP mode on next boot
	syspath.GELFSpoolDir(),                          // unshipped logs (may hold speech) must not ship with the next owner's key
}

// FactoryResetMinInterval is the minimum gap between two factory-reset
// triggers.
const FactoryResetMinInterval = 5 * time.Minute

// Single-flight + cooldown state shared across all trigger surfaces (HTTP /
// MQTT / GPIO).
var (
	factoryResetMu       sync.Mutex
	factoryResetInFlight bool
	factoryResetLastFire time.Time
)

// runFactoryReset is the trigger-agnostic worker.
func runFactoryReset(gw domain.AgentGateway) (started bool, errStatus int, errMessage string) {
	factoryResetMu.Lock()
	if factoryResetInFlight {
		factoryResetMu.Unlock()
		return false, http.StatusConflict, "factory-reset already running"
	}
	if !factoryResetLastFire.IsZero() {
		if wait := FactoryResetMinInterval - time.Since(factoryResetLastFire); wait > 0 {
			factoryResetMu.Unlock()
			return false, http.StatusTooManyRequests,
				fmt.Sprintf("factory-reset rate-limited, retry in %ds", int(wait.Seconds())+1)
		}
	}
	factoryResetInFlight = true
	factoryResetLastFire = time.Now()
	factoryResetMu.Unlock()

	log.Printf("[factory-reset] accepted — resetting active agent → wipe %d device paths + all runtime personas → reboot",
		len(deviceWipePaths))

	go func() {
		defer func() {
			factoryResetMu.Lock()
			factoryResetInFlight = false
			factoryResetMu.Unlock()
		}()

		if err := gw.ResetAgent(); err != nil {
			log.Printf("[factory-reset] agent reset error: %v (continuing with device wipe)", err)
		}

		wipeDeviceState()

		log.Printf("[factory-reset] all done — rebooting in 2s")
		if err := exec.Command("sh", "-c", "(sleep 2 && systemctl reboot) &").Start(); err != nil {
			log.Printf("[factory-reset] schedule reboot failed: %v", err)
		}
	}()

	return true, 0, ""
}

// wipeDeviceState removes per-device state independent of the agent backend
func wipeDeviceState() {
	log.Printf("[factory-reset] wiping %d device paths", len(deviceWipePaths))
	for _, p := range deviceWipePaths {
		osreset.WipePath("[factory-reset]", p)
	}
	wipeInactivePersonas()
}

// wipeInactivePersonas clears the persona + long-term memory of EVERY
// runtime, not only the one gw.ResetAgent() just handled.
func wipeInactivePersonas() {
	paths := migratepersona.PersonaPaths(migratepersona.DefaultOptions("", ""))
	log.Printf("[factory-reset] wiping %d persona paths across all runtimes", len(paths))
	for _, p := range paths {
		osreset.WipePath("[factory-reset/persona]", p)
	}
}

// FactoryReset performs a soft factory reset: wipe device state (config / API
// keys / enrollments / WiFi creds) + reboot.
func FactoryReset(c *gin.Context, gw domain.AgentGateway) {
	// Logged BEFORE runFactoryReset so even a rejected attempt (cooldown /
	// single-flight / failed auth) leaves a trail.
	authScheme := ""
	if h := c.GetHeader("Authorization"); h != "" {
		if i := strings.IndexByte(h, ' '); i > 0 {
			authScheme = h[:i] // e.g. "Bearer" — token deliberately not logged
		} else {
			authScheme = "present"
		}
	}
	_, cookieErr := c.Cookie("os_session")
	log.Printf("[factory-reset] TRIGGER received — remote=%s xff=%q x-real-ip=%q user-agent=%q auth=%q session-cookie=%v",
		c.Request.RemoteAddr,
		c.GetHeader("X-Forwarded-For"),
		c.GetHeader("X-Real-IP"),
		c.Request.UserAgent(),
		authScheme,
		cookieErr == nil,
	)

	started, status, msg := runFactoryReset(gw)
	if !started {
		if status == http.StatusTooManyRequests {
			factoryResetMu.Lock()
			wait := FactoryResetMinInterval - time.Since(factoryResetLastFire)
			factoryResetMu.Unlock()
			if wait > 0 {
				c.Header("Retry-After", strconv.Itoa(int(wait.Seconds())+1))
			}
		}
		c.JSON(status, serializers.ResponseError(msg))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(gin.H{
		"started":      true,
		"message":      "Soft factory reset started. Device will wipe its state and reboot into AP setup mode (~30s).",
		"device_wipes": deviceWipePaths,
	}))
}

// TriggerFactoryReset is the entry point for non-HTTP triggers (MQTT command
// handler, GPIO long-press service).
func TriggerFactoryReset(gw domain.AgentGateway) (started bool, reason string) {
	started, _, msg := runFactoryReset(gw)
	return started, msg
}
