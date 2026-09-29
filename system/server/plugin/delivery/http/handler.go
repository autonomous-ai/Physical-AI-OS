package http

import (
	// "encoding/json"  // Browse only — restore with it (#213)
	// "io"             // Browse only
	"log/slog"
	"net/http"
	// "time"           // Browse only

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/plugin"
	"go.autonomous.ai/os/system/server/serializers"
)

type PluginHandler struct {
	service *plugin.Service
}

func ProvidePluginHandler(ps *plugin.Service) PluginHandler {
	return PluginHandler{service: ps}
}

// Install handles POST /api/plugin/install. Clones, sets up venv, and creates
// a systemd unit. Runs async — returns 200 immediately. Poll List for status.
func (h *PluginHandler) Install(c *gin.Context) {
	var req domain.PluginInstallRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if req.URL == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("url is required"))
		return
	}

	go func() {
		if _, err := h.service.Install(req.URL); err != nil {
			slog.Error("[plugins] install failed", "component", "plugin-http", "url", req.URL, "error", err)
		}
	}()

	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// List handles GET /api/plugin. Returns all installed plugins.
func (h *PluginHandler) List(c *gin.Context) {
	c.JSON(http.StatusOK, serializers.ResponseSuccess(h.service.List()))
}

// Start handles POST /api/plugin/:name/start.
func (h *PluginHandler) Start(c *gin.Context) {
	name := c.Param("name")
	if err := h.service.Start(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// Stop handles POST /api/plugin/:name/stop.
func (h *PluginHandler) Stop(c *gin.Context) {
	name := c.Param("name")
	if err := h.service.Stop(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// Uninstall handles DELETE /api/plugin/:name.
func (h *PluginHandler) Uninstall(c *gin.Context) {
	name := c.Param("name")
	if err := h.service.Uninstall(name); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// Browse — PARKED, not deleted (#213): re-enable against our own catalog
// (skills.StoreGet) and re-register the route in server.go.
// func (h *PluginHandler) Browse(c *gin.Context) {
// 	const hfURL = "https://huggingface.co/api/spaces?filter=autonomous-os-plugin&full=true&sort=likes&direction=-1"
// 	client := &http.Client{Timeout: 10 * time.Second}
// 	resp, err := client.Get(hfURL)
// 	if err != nil {
// 		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to reach HuggingFace"))
// 		return
// 	}
// 	defer resp.Body.Close()
// 	body, err := io.ReadAll(resp.Body)
// 	if err != nil {
// 		c.JSON(http.StatusBadGateway, serializers.ResponseError("failed to read HuggingFace response"))
// 		return
// 	}
// 	var spaces []any
// 	if err := json.Unmarshal(body, &spaces); err != nil {
// 		c.JSON(http.StatusBadGateway, serializers.ResponseError("invalid JSON from HuggingFace"))
// 		return
// 	}
// 	c.JSON(http.StatusOK, serializers.ResponseSuccess(spaces))
// }
