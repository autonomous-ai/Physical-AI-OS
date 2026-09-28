package http

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	buddyjev "go.autonomous.ai/os/system/buddy/jev"
	"go.autonomous.ai/os/system/server/serializers"
)

type suggestionRequest struct {
	Goal string `json:"goal" binding:"required,max=2000"`
	App  string `json:"app,omitempty" binding:"omitempty,max=256"`
}

type suggestionDecide func(context.Context, string, buddyjev.Tree, buddyjev.Options) buddyjev.Result

// Suggest only observes the paired Mac and returns an advisory action. In
// particular, this endpoint must never dispatch perform_ui_action itself.
func (h *BuddyHandler) Suggest(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16<<10)
	var req suggestionRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid suggestion request"))
		return
	}
	if strings.TrimSpace(req.Goal) == "" || !utf8.ValidString(req.Goal) || !utf8.ValidString(req.App) ||
		utf8.RuneCountInString(req.Goal) > 2000 || utf8.RuneCountInString(req.App) > 256 ||
		(req.App != "" && strings.TrimSpace(req.App) == "") {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("goal must contain 1–2000 characters; app must be nonblank when supplied"))
		return
	}
	respond := func(result buddyjev.Result) { c.JSON(http.StatusOK, serializers.ResponseSuccess(result)) }
	if !buddyjev.Enabled {
		respond(buddyjev.Result{Reason: "disabled"})
		return
	}
	if h.suggestGate == nil || h.suggestSelector == nil || h.service == nil || h.config == nil {
		respond(buddyjev.Result{Reason: "unavailable"})
		return
	}
	// Serialize the whole observation/decision so competing suggestions cannot
	// invalidate one another's snapshots before inference has even finished.
	if !h.suggestGate.TryLock() {
		respond(buddyjev.Result{Reason: "busy"})
		return
	}
	defer h.suggestGate.Unlock()
	base := strings.TrimRight(strings.TrimSpace(h.config.LLMBaseURL), "/")
	key := strings.TrimSpace(h.config.LLMAPIKey)
	if base == "" || key == "" {
		respond(buddyjev.Result{Reason: "unconfigured"})
		return
	}
	respond(executeSuggestion(c.Request.Context(), req, buddyjev.Options{Enabled: true,
		Endpoint: base + "/jev/decisions", APIKey: key}, h.service.Dispatch, h.suggestSelector.Suggest))
}

func executeSuggestion(parent context.Context, req suggestionRequest, opts buddyjev.Options,
	dispatch observationDispatch, decide suggestionDecide) buddyjev.Result {
	if !opts.Enabled {
		return buddyjev.Result{Reason: "disabled"}
	}
	if parent.Err() != nil {
		return buddyjev.Result{Reason: "cancelled"}
	}
	ctx, cancel := context.WithTimeout(parent, 5*time.Second+buddyjev.Budget)
	defer cancel()
	params := map[string]any{"max_nodes": 150, "max_depth": 12}
	if req.App != "" {
		params["app"] = req.App
	}
	cmd := buddy.Command{ID: buddy.NewCommandID(), Action: "get_ui_tree", Params: params, TimeoutMs: 5000,
		IssuedAt: time.Now().UTC().Format(time.RFC3339), IssuedBy: "api:/api/buddy/suggest"}
	started := time.Now()
	captureCtx, captureCancel := context.WithTimeout(ctx, 5*time.Second)
	raw, err := dispatch(captureCtx, cmd)
	captureCancel()
	if err != nil || ctx.Err() != nil {
		return buddyjev.Result{Reason: "observation_unavailable"}
	}
	if len(raw) > 1<<20 {
		return buddyjev.Result{Reason: "invalid_observation"}
	}
	var response struct {
		ID     string         `json:"id"`
		OK     bool           `json:"ok"`
		Result *buddyjev.Tree `json:"result"`
	}
	if json.Unmarshal(raw, &response) != nil || response.ID != cmd.ID || !response.OK || response.Result == nil {
		return buddyjev.Result{Reason: "invalid_observation"}
	}
	if response.Result.ExpiresInMS <= 0 || response.Result.ExpiresInMS > 30000 {
		return buddyjev.Result{Reason: "stale_observation"}
	}
	// The TTL was computed on the Mac before transmission. Subtracting the full
	// round trip is conservative and prevents spending an already expired ref.
	response.Result.ExpiresInMS -= int(time.Since(started).Milliseconds())
	if response.Result.ExpiresInMS <= 0 {
		return buddyjev.Result{Reason: "stale_observation"}
	}
	result := decide(ctx, req.Goal, *response.Result, opts)
	if ctx.Err() != nil {
		return buddyjev.Result{Reason: "cancelled"}
	}
	return result
}
