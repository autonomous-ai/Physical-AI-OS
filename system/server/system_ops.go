package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/ota"
	"go.autonomous.ai/os/system/server/serializers"
)

// softwareUpdate installs the published version of one component now, via the
// bootstrap worker — the UI equivalent of `software-update <target>` over
// SSH.
func (s *Server) softwareUpdate(c *gin.Context) {
	target, err := ota.TriggerUpdate(c.Request.Context(), s.config, c.Param("target"))
	if err != nil {
		// 429 carries Retry-After so the web button can surface a useful
		// message instead of looking broken.
		var rl *ota.RateLimitedError
		if errors.As(err, &rl) {
			c.Header("Retry-After", strconv.Itoa(rl.RetryAfterSeconds()))
		}
		c.JSON(otaHTTPStatus(err), serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess("software update triggered: "+target))
}

// otaHTTPStatus maps an ota package error to the status the Versions card has
// always received for it.
func otaHTTPStatus(err error) int {
	var rl *ota.RateLimitedError
	switch {
	case errors.Is(err, ota.ErrUnknownTarget):
		return http.StatusBadRequest
	case errors.As(err, &rl):
		return http.StatusTooManyRequests
	case errors.Is(err, ota.ErrBuildRequest):
		return http.StatusInternalServerError
	default: // unreachable / refused / bad status / undecodable reply
		return http.StatusBadGateway
	}
}

// otaSecurity reports whether this device verifies OTA metadata and
// artifacts. GET /api/system/ota-security
func (s *Server) otaSecurity(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 5*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:8080/security", nil)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("build request: "+err.Error()))
		return
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap unreachable: "+err.Error()))
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("bootstrap security status: "+resp.Status))
		return
	}
	var status map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&status); err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("decode security status: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(status))
}

// otaVersions reports, per component, what this device runs vs what the OTA
// feed offers — so the Versions card can show an `update` button ONLY where
// an update actually exists instead of on every row.
func (s *Server) otaVersions(c *gin.Context) {
	versions, err := ota.Versions(c.Request.Context(), s.config)
	if err != nil {
		c.JSON(otaHTTPStatus(err), serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(versions))
}

// otaUpdating lists the components the bootstrap worker is installing right
// now, so the Versions card can label that row "updating…" while the work
// runs.
func (s *Server) otaUpdating(c *gin.Context) {
	updating, err := ota.Updating(c.Request.Context(), s.config)
	if err != nil {
		c.JSON(otaHTTPStatus(err), serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{"updating": updating}))
}

// execCommand runs a shell command (sh -c) and returns stdout, stderr, and exit code.
// POST /api/system/exec  body: {"cmd": "..."}
func (s *Server) execCommand(c *gin.Context) {
	var body struct {
		Cmd string `json:"cmd"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.Cmd) == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("cmd required"))
		return
	}

	ctx, cancel := context.WithTimeout(c.Request.Context(), 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, "sh", "-c", body.Cmd)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	exitCode := 0
	if err := cmd.Run(); err != nil {
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		} else {
			exitCode = -1
			if stderr.Len() == 0 {
				stderr.WriteString(err.Error())
			}
		}
	}

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"stdout":    stdout.String(),
		"stderr":    stderr.String(),
		"exit_code": exitCode,
	}))
}
