package mqtthandler

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
)

// connectorHTTPTimeout bounds a local-web PAT write.
const connectorHTTPTimeout = 2 * time.Minute

// patConnectorRequest is the body of POST /api/device/connectors/pat, sent by
// the device's local Settings UI.
type patConnectorRequest struct {
	Connector string `json:"connector"`
	APIKey    string `json:"api_key"`
	// UserEmail / UserID / PageID land in the connector entry's non-secret
	// fields so a subsequent GET can surface a "connected as <who>" hint
	// without exposing the token.
	UserEmail   string            `json:"user_email,omitempty"`
	Credentials map[string]string `json:"credentials,omitempty"`
}

// SetConnectorPAT handles POST /api/device/connectors/pat — the local
// Settings UI's write path for a static-credential connector (Facebook Fan
// Page, Gmail app password, …).
func (h *DeviceMQTTHandler) SetConnectorPAT(c *gin.Context) {
	var req patConnectorRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}
	req.Connector = strings.TrimSpace(req.Connector)
	req.APIKey = strings.TrimSpace(req.APIKey)
	if req.Connector == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("connector is required"))
		return
	}
	if req.APIKey == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("api_key is required"))
		return
	}

	if !validConnectorCode.MatchString(req.Connector) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}

	writer := h.connectorWriterFor(req.Connector)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}

	creds := ConnectorCreds{
		Connector:   req.Connector,
		AuthType:    "pat",
		APIKey:      req.APIKey,
		UserEmail:   strings.TrimSpace(req.UserEmail),
		Credentials: sanitizeCredentials(req.Credentials),
		// Static credentials never expire and nothing rotates them here.
		Refresh:    false,
		ExpiresAt:  0,
		ObtainedAt: time.Now().Unix(),
	}

	ctx, cancel := context.WithTimeout(context.Background(), connectorHTTPTimeout)
	defer cancel()
	if err := writer.Write(ctx, creds); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	_ = h.publishDataResult(
		"connector.set."+req.Connector,
		"success",
		"",
		map[string]any{
			"connector": req.Connector,
			"auth_type": "pat",
			"initiator": "device_local",
		},
	)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector":  req.Connector,
		"auth_type":  "pat",
		"user_email": creds.UserEmail,
	}))
}

// sanitizeCredentials copies only the string→string pairs whose keys the
// local UI is allowed to set.
func sanitizeCredentials(in map[string]string) map[string]string {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]string, len(in))
	for k, v := range in {
		key := strings.TrimSpace(k)
		val := strings.TrimSpace(v)
		if key == "" || val == "" {
			continue
		}
		out[key] = val
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// connectorInfoResponse is the read-side shape for GET
// /api/device/connectors/:code.
type connectorInfoResponse struct {
	Connector   string            `json:"connector"`
	Connected   bool              `json:"connected"`
	AuthType    string            `json:"auth_type,omitempty"`
	UserEmail   string            `json:"user_email,omitempty"`
	Credentials map[string]string `json:"credentials,omitempty"`
	ObtainedAt  int64             `json:"obtained_at,omitempty"`
}

// GetConnector handles GET /api/device/connectors/:code.
func (h *DeviceMQTTHandler) GetConnector(c *gin.Context) {
	code := strings.TrimSpace(c.Param("code"))
	if code == "" || !validConnectorCode.MatchString(code) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}
	if h.connectorWriter == nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{Connector: code, Connected: false}))
		return
	}
	creds, ok, err := h.connectorWriter.loadEntry(code)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	if !ok {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{Connector: code, Connected: false}))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(connectorInfoResponse{
		Connector:   code,
		Connected:   true,
		AuthType:    creds.AuthType,
		UserEmail:   creds.UserEmail,
		Credentials: creds.Credentials,
		ObtainedAt:  creds.ObtainedAt,
	}))
}

// RemoveConnector handles DELETE /api/device/connectors/:code.
func (h *DeviceMQTTHandler) RemoveConnector(c *gin.Context) {
	code := strings.TrimSpace(c.Param("code"))
	if code == "" || !validConnectorCode.MatchString(code) {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid connector code"))
		return
	}
	writer := h.connectorWriterFor(code)
	if writer == nil {
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("no writer available"))
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), connectorHTTPTimeout)
	defer cancel()
	removed, err := writer.Remove(ctx, code)
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	// Same fd_channel echo as connector.set above — a local admin
	// disconnect must reach BE so the connectors page on autonomous.ai flips
	// the row to Not connected.
	_ = h.publishDataResult(
		"connector.remove."+code,
		"success",
		"",
		map[string]any{
			"connector": code,
			"removed":   removed,
			"initiator": "device_local",
		},
	)

	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"connector": code,
		"removed":   removed,
	}))
}
