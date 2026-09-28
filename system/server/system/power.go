package system

import (
	"fmt"
	"log"
	"net/http"
	"sync"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

type powerAction string

const (
	powerActionReboot   powerAction = "reboot"
	powerActionShutdown powerAction = "shutdown"
)

var (
	// powerActionDelay gives the caller's HTTP or MQTT acknowledgement time to
	// leave the device before HAL starts its spoken power-off sequence.
	powerActionDelay = 2 * time.Second
	powerMu          sync.Mutex
	powerPending     powerAction
	powerRequest     = func(action powerAction) error {
		switch action {
		case powerActionReboot:
			return hal.RebootOS()
		case powerActionShutdown:
			return hal.ShutdownOS()
		default:
			return fmt.Errorf("unknown power action %q", action)
		}
	}
)

// triggerPower accepts one device-power operation and schedules its HAL call.
func triggerPower(action powerAction) (started bool, reason string) {
	powerMu.Lock()
	if powerPending != "" {
		pending := powerPending
		powerMu.Unlock()
		return false, "device power action already pending: " + string(pending)
	}
	powerPending = action
	powerMu.Unlock()

	go func() {
		time.Sleep(powerActionDelay)
		if err := powerRequest(action); err != nil {
			log.Printf("[system-power] %s request to HAL failed: %v", action, err)
			powerMu.Lock()
			powerPending = ""
			powerMu.Unlock()
		}
	}()
	return true, ""
}

// TriggerReboot queues HAL's cue-aware reboot action for a non-HTTP caller.
func TriggerReboot() (started bool, reason string) {
	return triggerPower(powerActionReboot)
}

// TriggerShutdown queues HAL's cue- and servo-aware shutdown action for a non-HTTP caller.
func TriggerShutdown() (started bool, reason string) {
	return triggerPower(powerActionShutdown)
}

func powerResponse(c *gin.Context, action powerAction) {
	started, reason := triggerPower(action)
	if !started {
		c.JSON(http.StatusConflict, serializers.ResponseError(reason))
		return
	}
	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(gin.H{
		"started": true,
		"action":  action,
		"message": "Device will " + string(action) + " shortly.",
	}))
}

// Reboot queues a reboot after the HTTP response is sent.
func Reboot(c *gin.Context) { powerResponse(c, powerActionReboot) }

// Shutdown queues a safe shutdown after the HTTP response is sent.
func Shutdown(c *gin.Context) { powerResponse(c, powerActionShutdown) }
