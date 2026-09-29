package server

import (
	"encoding/base64"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/vision"
)

// lookWidth/lookQuality shrink the JPEG (~50-80 KB instead of ~300-500 KB at
// full 1920x1080) so the vision model uploads and tokenizes faster.
const (
	lookWidth   = 768
	lookQuality = 75
)

// lookRequest is the body of POST /api/vision/look.
type lookRequest struct {
	// Question is what the user asked, so the vision model answers it instead
	// of narrating the frame generically. Optional.
	Question string `json:"question"`
}

// lookAndDescribe captures a frame and hands back text the agent can actually
// read — one call, no branching for the agent to get wrong.
func (s *Server) lookAndDescribe(c *gin.Context) {
	var req lookRequest
	_ = c.ShouldBindJSON(&req)

	path, err := hal.Snapshot(lookWidth, lookQuality)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot failed: "+err.Error()))
		return
	}
	if vision.ModelSupportsVision(s.config) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path}))
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot not readable"))
		return
	}
	desc, err := vision.DescribeWithRetry(s.config, base64.StdEncoding.EncodeToString(data), req.Question)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("describe failed: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path, "description": desc}))
}
