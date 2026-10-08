package http

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func TestSuppressedFillerRunCannotResume(t *testing.T) {
	fm := NewFillerManager()
	id := "silent-followup"
	fm.SuppressRun(id)
	for _, delegated := range []bool{false, true} {
		if delegated {
			fm.MarkDelegatedVoiceRun(id, "interaction")
		} else {
			fm.MarkVoiceRun(id, "interaction")
		}
		fm.OnTurnStart(id)
		fm.OnToolStart(id, `{}`, "search")
		fm.OnToolEnd(id)
		if fm.HasActiveRun(id) || fm.voiceRuns[id] || fm.delegated[id] || fm.interactions[id] != "" {
			t.Fatal("suppressed follow-up became eligible for automatic fillers")
		}
		fm.Cancel(id)
	}
	// Suppression must not affect a later conversation opener.
	fm.MarkVoiceRun("new-opener", "")
	fm.OnTurnStart("new-opener")
	defer fm.Cancel("new-opener")
	if !fm.HasActiveRun("new-opener") {
		t.Fatal("ordinary voice run lost fillers")
	}
}

func TestFillerSuppressionRetentionIsBounded(t *testing.T) {
	fm := NewFillerManager()
	fm.SuppressRun("")
	for i := 0; i < maxSuppressedFillerRuns+10; i++ {
		id := fmt.Sprint(i)
		fm.SuppressRun(id)
		fm.SuppressRun(id)
	}
	if len(fm.suppressed) != maxSuppressedFillerRuns || len(fm.suppressedOrder) != maxSuppressedFillerRuns || fm.suppressed["0"] || !fm.suppressed[fmt.Sprint(maxSuppressedFillerRuns+9)] {
		t.Fatal("suppression must retain only the most recent distinct runs")
	}
}

func TestSuppressedFillerRunConcurrentLifecycle(t *testing.T) {
	fm := NewFillerManager()
	const id = "silent-concurrent-followup"
	fm.SuppressRun(id)
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			fm.SuppressRun(id)
			fm.MarkDelegatedVoiceRun(id, "interaction")
			fm.OnTurnStart(id)
			fm.OnToolStart(id, `{}`, "search")
			fm.OnToolEnd(id)
			fm.Cancel(id)
		}()
	}
	wg.Wait()
	if fm.HasActiveRun(id) || fm.voiceRuns[id] || fm.delegated[id] {
		t.Fatal("concurrent lifecycle revived a suppressed filler")
	}
}

func TestSensingFillerSuppressionPayloadDefault(t *testing.T) {
	var req SensingEventRequest
	if err := json.Unmarshal([]byte(`{"type":"voice","message":"hello"}`), &req); err != nil {
		t.Fatal(err)
	}
	if req.SuppressAutoFillers {
		t.Fatal("legacy payload must retain fillers")
	}
	if err := json.Unmarshal([]byte(`{"suppress_auto_fillers":true}`), &req); err != nil {
		t.Fatal(err)
	}
	if !req.SuppressAutoFillers {
		t.Fatal("HAL flag was not decoded")
	}
}

func TestSensingSuppressionSurvivesQueueAndMainDispatch(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, queued := range []bool{false, true} {
		t.Run(fmt.Sprint(queued), func(t *testing.T) {
			previous := DefaultFillerManager
			DefaultFillerManager = NewFillerManager()
			defer func() { DefaultFillerManager = previous }()
			h := &SensingHandler{monitorBus: monitor.ProvideBus(), config: &config.Config{}}
			runID := "context-run"
			direct := &contextGateway{}
			pending := &queuedVoiceGateway{}
			if queued {
				h.agentGateway = pending
				runID = "queued-voice-run"
			} else {
				h.agentGateway = direct
			}
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", strings.NewReader(`{"type":"voice","message":"what about tomorrow?","suppress_auto_fillers":true}`))
			c.Request.Header.Set("Content-Type", "application/json")
			h.PostEvent(c)
			if rec.Code != http.StatusOK {
				t.Fatalf("dispatch failed: %s", rec.Body.String())
			}
			if queued && pending.fixedRun != runID {
				t.Fatal("queue lost fixed run ID")
			}
			if !queued && !strings.Contains(direct.sent, "what about tomorrow?") {
				t.Fatal("suppression blocked actual agent input")
			}
			if !DefaultFillerManager.suppressed[runID] {
				t.Fatal("dispatch lost suppression")
			}
			// Runtime replay and delegated task resume retain this same run ID.
			DefaultFillerManager.MarkVoiceRun(runID, "")
			DefaultFillerManager.OnTurnStart(runID)
			DefaultFillerManager.OnToolEnd(runID)
			if DefaultFillerManager.HasActiveRun(runID) {
				t.Fatal("replay resumed automatic fillers")
			}
		})
	}
}
