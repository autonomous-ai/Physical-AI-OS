package server

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

func (s *Server) initializeHarnessVoice(ctx context.Context) {
	s.harnessVoiceCtx = ctx
	if s.harnessVoice == nil && s.harnessService != nil {
		s.harnessVoice = harness.NewVoiceController(s.harnessService, harness.VoiceCallbacks{
			SupportsMode: hal.SupportsHarnessVoiceMode,
			OnDispatchRequest: func(agentID, runID string, frame harness.Frame) {
				s.registerHarnessDispatch(agentID, runID, false, false, frame)
			},
			OnResponse: s.deliverHarnessVoiceQuestion,
		})
		s.harnessVoice.Start(ctx)
	}
	s.deviceMQTTHandler.SetHarnessVoiceController(s.harnessVoice)
}

func (s *Server) registerHarnessVoiceRoutes(group *gin.RouterGroup) {
	group.POST("voice-mode/gesture", localOnlyMiddleware(), s.handleHarnessVoiceGesture)
	group.POST("voice-mode/focus", localOnlyMiddleware(), s.handleHarnessVoiceFocusGesture)
	group.GET("voice-mode", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusOK, serializers.ResponseSuccess(s.harnessVoice.SupportState(c.Request.Context())))
	})
	group.PUT("voice-mode", adminAuthMiddleware(s.config), func(c *gin.Context) {
		var req struct {
			Enabled *bool   `json:"enabled" binding:"required"`
			AgentID *string `json:"agentId"`
		}
		if err := bindHarnessVoiceJSON(c, &req); err != nil {
			return
		}
		if req.AgentID != nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("Choose the focused agent in the Harness app, not the device web UI"))
			return
		}
		state, err := s.harnessVoice.SetMode(c.Request.Context(), *req.Enabled)
		writeHarnessVoiceResult(c, state, err)
	})
	group.GET("agents", adminAuthMiddleware(s.config), func(c *gin.Context) {
		result, err := s.harnessVoice.Agents(c.Request.Context())
		writeHarnessVoiceResult(c, result, err)
	})
	group.GET("voice-mode/question", adminAuthMiddleware(s.config), func(c *gin.Context) {
		result, err := s.harnessVoice.Question(c.Request.Context())
		writeHarnessVoiceResult(c, result, err)
	})
	group.POST("voice-mode/receipt", adminAuthMiddleware(s.config), func(c *gin.Context) {
		result, err := s.harnessVoice.Receipt(c.Request.Context())
		writeHarnessVoiceResult(c, result, err)
	})
	group.POST("voice-mode/resolve", adminAuthMiddleware(s.config), func(c *gin.Context) {
		var req struct {
			Resolution     string `json:"resolution" binding:"required"`
			IdempotencyKey string `json:"idempotencyKey" binding:"required"`
		}
		if err := bindHarnessVoiceJSON(c, &req); err != nil {
			return
		}
		err := s.harnessVoice.Resolve(req.Resolution, req.IdempotencyKey)
		writeHarnessVoiceResult(c, s.harnessVoice.State(), err)
	})
	group.POST("voice-mode/answer", adminAuthMiddleware(s.config), func(c *gin.Context) {
		var req struct {
			QuestionRequestID string            `json:"questionRequestId" binding:"required"`
			FocusRevision     string            `json:"focusRevision" binding:"required"`
			Answers           map[string]string `json:"answers" binding:"required"`
		}
		if err := bindHarnessVoiceJSON(c, &req); err != nil {
			return
		}
		// An answer is a new turn. The controller validates the live question and
		// uses the existing asynchronous Harness response path for its result.
		runID := newHarnessVoiceRunID("")
		interactionID := telemetry.ReportTaskStarted("web_chat", "", runID)
		err := s.harnessVoice.Answer(c.Request.Context(), req.QuestionRequestID, req.Answers, runID, req.FocusRevision)
		reportHarnessDispatchError(runID, interactionID, err)
		writeHarnessVoiceResult(c, gin.H{"runId": runID}, err)
	})
}

func bindHarnessVoiceJSON(c *gin.Context, dst any) error {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 32*1024)
	if err := c.ShouldBindJSON(dst); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("Invalid Harness voice request"))
		return err
	}
	return nil
}

func writeHarnessVoiceResult(c *gin.Context, result any, err error) {
	c.Header("Cache-Control", "no-store")
	if err != nil {
		c.JSON(http.StatusConflict, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
}

func newHarnessVoiceRunID(interactionID string) string {
	if interactionID == "" {
		var nonce [16]byte
		if _, err := rand.Read(nonce[:]); err != nil {
			// crypto/rand failure is fatal on supported Go platforms.
			panic(err)
		}
		interactionID = hex.EncodeToString(nonce[:])
	}
	digest := sha256.Sum256([]byte(interactionID))
	return "device-harness-" + hex.EncodeToString(digest[:16])
}

// handleHarnessVoice runs before local intents and main-runtime readiness/busy
// checks. Only HAL's explicit capture snapshot opts in; typed/MQTT chat and
// ambient sensing keep their existing routes.
func (s *Server) handleHarnessVoice(c *gin.Context, req sensinghttp.SensingEventRequest) bool {
	if req.HarnessVoice == nil || (req.Type != "voice" && req.Type != "voice_command" && req.Type != "voice_followup") {
		return false
	}
	if !isLoopbackHost(hostOnly(c.Request.RemoteAddr)) ||
		(c.GetHeader("X-Forwarded-For") != "" && !isLoopbackHost(firstForwardedFor(c.GetHeader("X-Forwarded-For")))) ||
		(c.GetHeader("X-Real-IP") != "" && !isLoopbackHost(strings.TrimSpace(c.GetHeader("X-Real-IP")))) {
		c.JSON(http.StatusForbidden, serializers.ResponseError("Harness voice input is local-only"))
		return true
	}
	if s.harnessVoice == nil {
		if !req.HarnessVoice.Enabled {
			return false
		}
		c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("Harness voice service unavailable"))
		return true
	}
	state := s.harnessVoice.State()
	if !req.HarnessVoice.Enabled && !state.Enabled && req.HarnessVoice.Generation == state.Generation {
		return false
	}
	runID := newHarnessVoiceRunID(req.InteractionID)
	if err := s.beginHarnessHistory(runID, req.Message, state); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not save Harness conversation input"))
		return true
	}
	interactionID := telemetry.ReportTaskStarted(req.Type, req.InteractionID, runID)
	start := flow.Start("sensing_input", map[string]any{
		"type": req.Type, "message": req.Message, "interaction_id": interactionID, "route": "harness_only",
	}, runID)
	// Return promptly: HAL's sender times out at five seconds. The controller
	// reserves one mutation and deduplicates retries by this stable local run ID.
	parent := s.harnessVoiceCtx
	if parent == nil {
		parent = context.Background()
	}
	safego.Go("harness-voice", func() {
		ctx, cancel := context.WithTimeout(parent, 70*time.Second)
		defer cancel()
		var err error
		if !req.HarnessVoice.Enabled {
			err = errors.New("Harness voice mode changed during this utterance. Please speak again")
		} else {
			err = s.harnessVoice.Submit(ctx, req.Message, runID, req.HarnessVoice.Generation)
		}
		payload := map[string]any{"route": "harness_only"}
		if err != nil {
			reportHarnessDispatchError(runID, interactionID, err)
			payload["error"] = err.Error()
			slog.Warn("Harness voice dispatch", "component", "harness", "run_id", runID, "error", err)
			var uncertain *harness.DeliveryUnknownError
			if errors.As(err, &uncertain) {
				// Keep the original route pending: the real result can still arrive.
				if s.agentHandler != nil && s.hasHarnessReply(state.AgentID, runID) {
					s.agentHandler.DeliverHarnessProgress(runID, err.Error())
					s.deliverHarnessVoiceMessage("harness-notice", runID+"-notice", err.Error())
				}
			} else {
				s.deliverHarnessVoiceMessage(state.AgentID, runID, err.Error())
			}
		}
		flow.End("sensing_input", start, payload, runID)
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"runId": runID, "handler": "harness"}))
	return true
}

func (s *Server) deliverHarnessVoiceMessage(agentID, runID, text string) {
	if agentID == "" {
		agentID = "harness-voice"
	}
	if !s.hasHarnessReply(agentID, runID) {
		// A preflight failure is a local notice, not another remote task.
		s.registerHarnessRoute(agentID, runID, false, false, "", true)
	}
	if s.agentHandler != nil {
		s.agentHandler.MarkHarnessLocalResponseRun(runID, false)
	}
	s.deliverHarnessFinal(agentID, runID, text)
}
