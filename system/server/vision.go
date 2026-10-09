package server

import (
	"encoding/base64"
	"net/http"
	"os"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/vision"
)

// lookWidth/lookQuality shrink the JPEG (~50-80 KB instead of ~300-500 KB at
// full 1920x1080) so the vision model uploads and tokenizes faster.
// A read_text look uses lookReadWidth: at 768 px small labels are illegible.
const (
	lookWidth     = 768
	lookReadWidth = 1280
	lookQuality   = 75
)

// The spoken cue before the photo is a cached phrase of about a second; these
// bound how long the shutter waits for it so a stuck speaker never stalls a look.
const (
	cueMaxWait    = 2500 * time.Millisecond
	cueStartGrace = 600 * time.Millisecond
)

// Seams for tests.
var (
	cueSpeakerBusy = hal.SpeakerBusy
	cuePoll        = 100 * time.Millisecond
)

// lookRequest is the body of POST /api/vision/look.
type lookRequest struct {
	// Question is what the user asked, so the vision model answers it instead
	// of narrating the frame generically. Optional.
	Question string `json:"question"`
	// ReadText asks to read text, labels or a brand: a sharper frame and the
	// vision model's thinking, ~3x slower. Optional.
	ReadText bool `json:"read_text"`
}

// lookFailedHint leads the describe-failure error: the agent reads the tool
// output when it picks its next step, and a skill rule alone did not stop it from
// re-snapshotting into vision_analyze (same vision model, +22-88 s on lamp-52e6).
const lookFailedHint = "The vision model could not answer in time. Tell the user you couldn't see it this time and stop: " +
	"do not take another snapshot or call vision_analyze or any other image tool, they use the same model. "

// lookAndDescribe captures a frame and hands back text the agent can actually
// read — one call, no branching for the agent to get wrong.
func (s *Server) lookAndDescribe(c *gin.Context) {
	var req lookRequest
	_ = c.ShouldBindJSON(&req)

	// On a voice turn, say that a photo is coming and let the line finish before
	// the shutter; then say it was taken, since describing it takes far longer.
	if sensinghttp.DefaultFillerManager.SayInVoiceRun("look_capturing_main") {
		waitForCue(cueMaxWait, cueStartGrace)
	}
	width := lookWidth
	if req.ReadText {
		width = lookReadWidth
	}
	path, err := hal.Snapshot(width, lookQuality)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot failed: "+err.Error()))
		return
	}
	sensinghttp.DefaultFillerManager.SayInVoiceRun("look_analyzing")
	if vision.ModelSupportsVision(s.config) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path}))
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError("snapshot not readable"))
		return
	}
	desc, err := vision.LookWithRetry(s.config, base64.StdEncoding.EncodeToString(data), req.Question, req.ReadText)
	if err != nil {
		c.JSON(http.StatusBadGateway, serializers.ResponseError(lookFailedHint+"describe failed: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"path": path, "description": desc}))
}

// waitForCue blocks until the speaker has played the cue and gone quiet. It
// gives up after startGrace if speech never starts, and after maxWait overall.
func waitForCue(maxWait, startGrace time.Duration) {
	start := time.Now()
	started := false
	for time.Since(start) < maxWait {
		if cueSpeakerBusy() {
			started = true
		} else if started || time.Since(start) >= startGrace {
			return
		}
		time.Sleep(cuePoll)
	}
}
