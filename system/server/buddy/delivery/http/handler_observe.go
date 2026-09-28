package http

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"image/jpeg"
	"math"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/vision"
)

const maxDesktopJPEG = 12 << 20

type observationRequest struct {
	Question  string   `json:"question" binding:"required,max=2000"`
	App       *string  `json:"app" binding:"omitempty,max=256"`
	WindowID  *uint32  `json:"window_id" binding:"omitempty,gt=0"`
	DisplayID *uint32  `json:"display_id" binding:"omitempty,gt=0"`
	Scale     *float64 `json:"scale" binding:"omitempty,gte=0.01,lte=1"`
}

type desktopObservation struct {
	Description string         `json:"description"`
	Screenshot  map[string]any `json:"screenshot"`
}

type observationDispatch func(context.Context, buddy.Command) (json.RawMessage, error)
type observationDescribe func(context.Context, string, string) (string, error)

func (r observationRequest) validate() error {
	if strings.TrimSpace(r.Question) == "" || !utf8.ValidString(r.Question) || utf8.RuneCountInString(r.Question) > 2000 {
		return fmt.Errorf("question must contain 1–2000 characters")
	}
	if r.App != nil && (strings.TrimSpace(*r.App) == "" || !utf8.ValidString(*r.App) || utf8.RuneCountInString(*r.App) > 256) {
		return fmt.Errorf("app must contain 1–256 characters")
	}
	if r.WindowID != nil && (*r.WindowID == 0 || r.App == nil) {
		return fmt.Errorf("window_id must be positive and requires app")
	}
	if r.App != nil && r.DisplayID != nil {
		return fmt.Errorf("app and display_id are mutually exclusive")
	}
	if r.DisplayID != nil && *r.DisplayID == 0 {
		return fmt.Errorf("display_id must be a positive uint32")
	}
	if r.Scale != nil && (math.IsNaN(*r.Scale) || math.IsInf(*r.Scale, 0) || *r.Scale < .01 || *r.Scale > 1) {
		return fmt.Errorf("scale must be between 0.01 and 1")
	}
	return nil
}

// Observe captures the paired Mac and asks the configured auxiliary vision
// model to describe it. The route must use the same local-only gate as Command.
func (h *BuddyHandler) Observe(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16<<10)
	var req observationRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	if err := req.validate(); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	result, err := executeObservation(c.Request.Context(), req, h.service.Dispatch,
		func(ctx context.Context, image, question string) (string, error) {
			return vision.DescribeDesktop(ctx, h.config, image, question)
		})
	if err != nil {
		status := http.StatusBadGateway
		if errors.Is(err, context.DeadlineExceeded) {
			status = http.StatusGatewayTimeout
		}
		c.JSON(status, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
}

func executeObservation(parent context.Context, req observationRequest, dispatch observationDispatch, describe observationDescribe) (*desktopObservation, error) {
	if err := req.validate(); err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(parent, 80*time.Second)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	scale := .5
	if req.App != nil {
		scale = 1 // Window captures already omit the surrounding desktop; preserve small text.
	}
	if req.Scale != nil {
		scale = *req.Scale
	}
	params := map[string]any{"scale": scale, "return_format": "base64"}
	if req.DisplayID != nil {
		params["display_id"] = *req.DisplayID
	}
	if req.App != nil {
		params["app"] = *req.App
	}
	if req.WindowID != nil {
		params["window_id"] = *req.WindowID
	}
	cmd := buddy.Command{ID: buddy.NewCommandID(), Action: "screenshot", Params: params, TimeoutMs: 15000,
		IssuedAt: time.Now().UTC().Format(time.RFC3339), IssuedBy: "api:/api/buddy/observe"}
	captureCtx, cancelCapture := context.WithTimeout(ctx, 20*time.Second)
	raw, err := dispatch(captureCtx, cmd)
	cancelCapture()
	if err != nil {
		return nil, fmt.Errorf("capture desktop: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	image, metadata, err := validateDesktopScreenshot(raw, cmd.ID)
	if err != nil {
		return nil, err
	}
	// Older companions may ignore unknown targeting fields. Never describe a
	// full display as the requested window when the capture contract is absent.
	if req.App != nil {
		windowID, ok := metadata["window_id"].(float64)
		if metadata["capture_scope"] != "window" || !ok || windowID <= 0 ||
			(req.WindowID != nil && windowID != float64(*req.WindowID)) {
			return nil, fmt.Errorf("companion did not capture the requested window; update Buddy or use an explicit display capture")
		}
	}
	description, err := describe(ctx, image, req.Question)
	if err != nil {
		return nil, fmt.Errorf("describe desktop: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(description) == "" {
		return nil, fmt.Errorf("desktop description is empty")
	}
	return &desktopObservation{Description: strings.TrimSpace(description), Screenshot: metadata}, nil
}

func validateDesktopScreenshot(raw json.RawMessage, commandID string) (string, map[string]any, error) {
	if len(raw) > base64.StdEncoding.EncodedLen(maxDesktopJPEG)+(64<<10) {
		return "", nil, fmt.Errorf("desktop screenshot payload exceeds limit")
	}
	var response struct {
		ID     string         `json:"id"`
		OK     bool           `json:"ok"`
		Result map[string]any `json:"result"`
		Error  string         `json:"error"`
	}
	if err := json.Unmarshal(raw, &response); err != nil {
		return "", nil, fmt.Errorf("decode desktop capture: %w", err)
	}
	if response.ID != commandID {
		return "", nil, fmt.Errorf("desktop capture response ID mismatch")
	}
	if !response.OK {
		return "", nil, fmt.Errorf("desktop capture failed: %s", response.Error)
	}
	encoded, ok := response.Result["image_b64"].(string)
	if !ok || encoded == "" || len(encoded) > base64.StdEncoding.EncodedLen(maxDesktopJPEG) {
		return "", nil, fmt.Errorf("desktop capture missing or oversized image")
	}
	if response.Result["mime"] != "image/jpeg" {
		return "", nil, fmt.Errorf("desktop capture must be image/jpeg")
	}
	data, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || len(data) == 0 || len(data) > maxDesktopJPEG {
		return "", nil, fmt.Errorf("desktop capture has invalid base64 JPEG")
	}
	config, err := jpeg.DecodeConfig(bytes.NewReader(data))
	if err != nil {
		return "", nil, fmt.Errorf("desktop capture has invalid JPEG header: %w", err)
	}
	if config.Width < 1 || config.Height < 1 || config.Width > 16384 || config.Height > 16384 || int64(config.Width)*int64(config.Height) > 40_000_000 {
		return "", nil, fmt.Errorf("desktop capture dimensions exceed limits")
	}
	if response.Result["width"] != float64(config.Width) || response.Result["height"] != float64(config.Height) {
		return "", nil, fmt.Errorf("desktop capture dimensions do not match JPEG")
	}
	metadata := make(map[string]any)
	for _, key := range []string{"path", "width", "height", "display_id", "display_scale", "display_origin_x", "display_origin_y", "point_width", "point_height", "capture_scale", "image_to_global_points", "bytes", "mime", "capture_scope", "backend", "window_id", "pid", "window_bounds"} {
		if value, exists := response.Result[key]; exists {
			metadata[key] = value
		}
	}
	return encoded, metadata, nil
}
