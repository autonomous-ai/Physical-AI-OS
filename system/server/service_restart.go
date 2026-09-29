package server

import (
	"context"
	"log"
	"net/http"
	"os/exec"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/serializers"
)

func restartCommand(ctx context.Context, name string, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, name, args...).CombinedOutput()
}

func serviceRestartHandler(run func(context.Context, string, ...string) ([]byte, error)) gin.HandlerFunc {
	return func(c *gin.Context) {
		target := c.Param("target")
		if target != "hal" && target != "os-server" {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("unsupported service restart target"))
			return
		}

		ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
		defer cancel()
		output, err := run(ctx, "systemd-run", "--collect", "--on-active=2s", "systemctl", "restart", target)
		if err != nil {
			log.Printf("[service-restart] schedule %s restart: %v: %s", target, err, output)
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("could not schedule service restart"))
			return
		}
		log.Printf("[service-restart] scheduled %s restart: %s", target, output)
		c.JSON(http.StatusAccepted, serializers.ResponseSuccess(gin.H{"target": target, "scheduled": true}))
	}
}
