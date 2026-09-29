package http

import (
	"errors"
	"net/http"
	"path/filepath"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/agentfile"
)

// What may leave the device is decided in system/agentfile (shared with MQTT chat.file.get);
// this handler is only the HTTP wrapper.

// ServeFile handles GET /api/agent/file?path=<absolute path>. The path is hostile input (see
// agentfile.Resolve); returns bare status codes since the only caller is an <img>/download link.
func (h *AgentHandler) ServeFile(c *gin.Context) {
	path, contentType, err := agentfile.Resolve(c.Query("path"), agentfile.Roots())
	if err != nil {
		switch {
		case errors.Is(err, agentfile.ErrType), errors.Is(err, agentfile.ErrOutsideRoots):
			c.Status(http.StatusForbidden)
		default:
			c.Status(http.StatusNotFound)
		}
		return
	}

	disposition := "attachment"
	if agentfile.Inline(path) {
		disposition = "inline"
	}
	// Basename only; nothing downstream should derive a directory from a header.
	c.Header("Content-Disposition", disposition+`; filename="`+filepath.Base(path)+`"`)
	c.Header("Content-Type", contentType)
	// The whitelist decides the type; never let a sniffed one override it.
	c.Header("X-Content-Type-Options", "nosniff")
	c.File(path)
}
