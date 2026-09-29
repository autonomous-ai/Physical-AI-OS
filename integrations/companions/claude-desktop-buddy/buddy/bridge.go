package buddy

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"claude-desktop-buddy/httpapi"
)

// claudeBrand is the Claude icon color used for buddy LED cues.
var claudeBrand = [3]int{193, 95, 60}

// Bridge maps buddy state changes to device and OS server HTTP calls.
type Bridge struct {
	halURL    string
	deviceURL string
	client    *http.Client
}

func NewBridge(halURL, deviceURL string) *Bridge {
	return &Bridge{
		halURL:    halURL,
		deviceURL: deviceURL,
		client:    &http.Client{Timeout: 5 * time.Second},
	}
}

// OnStateChange is called by StateMachine when state transitions.
func (b *Bridge) OnStateChange(old, next BuddyState, hb *Heartbeat) {
	log.Printf("[bridge] %s → %s", old, next)

	switch next {
	case StateSleep:
		// Hand the strip back to the user's LED state instead of turning it off.
		b.ledRestore()
		b.displayEyes("sleepy")

	case StateIdle:
		// Skip restore when leaving Busy; the following emotion restores itself.
		if old != StateBusy {
			b.ledRestore()
		}
		b.displayEyesMode()

	case StateBusy:
		b.ledEffect("pulse", claudeBrand, 0.8, 0)
		if hb != nil {
			b.displayInfo(
				fmt.Sprintf("%s tokens", formatTokens(hb.TokensToday)),
				fmt.Sprintf("%d sessions running", hb.Running),
			)
		}

	case StateAttention:
		b.ledEffect("blink", claudeBrand, 1.5, 0)
		if hb != nil && hb.Prompt != nil {
			b.displayInfo(
				fmt.Sprintf("Approve %s?", hb.Prompt.Tool),
				truncate(hb.Prompt.Hint, 40),
			)
			b.postSensingEvent(hb.Prompt)
		}

	case StateHeart:
		b.ledSolid(claudeBrand)
		b.displayEyes("happy")

	case StateCelebrate:
		b.ledEffect("rainbow", claudeBrand, 2.0, 3000)
		b.displayEyes("excited")
	}

	b.postBuddyState(next, hb)
}

// All Buddy LED writes are transient; ledRestore repaints the user's saved LED state.

func (b *Bridge) ledOff() {
	b.post(b.halURL+"/led/off", map[string]interface{}{
		"transient": true,
	})
}

func (b *Bridge) ledSolid(color [3]int) {
	b.post(b.halURL+"/led/solid", map[string]interface{}{
		"color":     color,
		"transient": true,
	})
}

func (b *Bridge) ledEffect(effect string, color [3]int, speed float64, durationMs int) {
	payload := map[string]interface{}{
		"effect":    effect,
		"color":     color,
		"speed":     speed,
		"transient": true,
	}
	if durationMs > 0 {
		payload["duration_ms"] = durationMs
	}
	b.post(b.halURL+"/led/effect", payload)
}

func (b *Bridge) ledRestore() {
	b.post(b.halURL+"/led/restore", nil)
}

func (b *Bridge) displayInfo(text, subtitle string) {
	b.post(b.halURL+"/display/info", map[string]interface{}{
		"text":     text,
		"subtitle": subtitle,
	})
}

func (b *Bridge) displayEyes(expression string) {
	b.post(b.halURL+"/display/eyes", map[string]interface{}{
		"expression": expression,
	})
}

func (b *Bridge) displayEyesMode() {
	b.post(b.halURL+"/display/eyes-mode", nil)
}

// postBuddyState sends buddy state to the OS server monitor bus.
func (b *Bridge) postBuddyState(state BuddyState, hb *Heartbeat) {
	detail := map[string]interface{}{
		"state": string(state),
	}
	if hb != nil && hb.Prompt != nil {
		detail["tool"] = hb.Prompt.Tool
		detail["hint"] = hb.Prompt.Hint
	}

	b.post(b.deviceURL+"/api/monitor/event", map[string]interface{}{
		"type":    "buddy_state",
		"summary": fmt.Sprintf("buddy: %s", state),
		"detail":  detail,
	})
}

// postSensingEvent sends approval event to the OS server sensing pipeline.
func (b *Bridge) postSensingEvent(prompt *Prompt) {
	b.post(b.deviceURL+"/api/sensing/event", map[string]interface{}{
		"type":    "buddy_approval",
		"message": fmt.Sprintf("Claude Desktop needs approval: %s on %s [prompt_id:%s]", prompt.Tool, prompt.Hint, prompt.ID),
	})
}

// announceCodeApproval cues the device (LED, display, claude_code_approval sensing event) so the agent asks the user; fire-and-forget.
func (b *Bridge) announceCodeApproval(req httpapi.CodeApprovalRequest) {
	b.ledEffect("blink", claudeBrand, 1.5, 0)
	b.displayInfo(
		fmt.Sprintf("Approve %s?", req.Tool),
		truncate(req.Hint, 40),
	)
	b.post(b.deviceURL+"/api/sensing/event", map[string]interface{}{
		"type": "claude_code_approval",
		"message": fmt.Sprintf("Claude Code needs approval: %s — %s [prompt_id:%s]",
			req.Tool, req.Hint, req.ID),
	})
}

// restoreAfterCodeApproval repaints the user's LED and eyes after a code approval resolves.
func (b *Bridge) restoreAfterCodeApproval() {
	b.ledRestore()
	b.displayEyesMode()
}

// expressEmotion triggers a coordinated LED + servo emotion on the device.
func (b *Bridge) expressEmotion(name string, intensity float64) {
	if name == "" {
		return
	}
	b.post(b.halURL+"/emotion", map[string]interface{}{
		"emotion":   name,
		"intensity": intensity,
	})
}

// prerenderTTS asks the device to synthesize and cache a phrase without playing it.
func (b *Bridge) prerenderTTS(text string) {
	if text == "" {
		return
	}
	b.post(b.halURL+"/voice/speak", map[string]interface{}{
		"text":      text,
		"prerender": true,
	})
}

// speakTTS posts a cached narration phrase to the device's /voice/speak; 409/503 responses are ignored.
func (b *Bridge) speakTTS(text string) {
	if text == "" {
		return
	}
	b.post(b.halURL+"/voice/speak", map[string]interface{}{
		"text":   text,
		"cached": true,
	})
}

// OnEvent forwards a parsed Event to the OS server monitor bus as buddy_event; fire-and-forget.
func (b *Bridge) OnEvent(evt *Event) {
	if evt == nil {
		return
	}
	b.post(b.deviceURL+"/api/monitor/event", map[string]interface{}{
		"type":    "buddy_event",
		"summary": fmt.Sprintf("buddy %s %s", evt.Evt, evt.Role),
		"detail": map[string]interface{}{
			"evt":     evt.Evt,
			"role":    evt.Role,
			"content": evt.TurnText(),
		},
	})
}

func (b *Bridge) post(url string, payload interface{}) {
	var body []byte
	if payload != nil {
		var err error
		body, err = json.Marshal(payload)
		if err != nil {
			log.Printf("[bridge] marshal error for %s: %v", url, err)
			return
		}
	}

	var resp *http.Response
	var err error
	if body != nil {
		resp, err = b.client.Post(url, "application/json", bytes.NewReader(body))
	} else {
		resp, err = b.client.Post(url, "application/json", nil)
	}
	if err != nil {
		log.Printf("[bridge] %s error: %v", url, err)
		return
	}
	resp.Body.Close()
}

func formatTokens(n int) string {
	if n >= 1000 {
		return fmt.Sprintf("%.1fK", float64(n)/1000)
	}
	return fmt.Sprintf("%d", n)
}

// truncate shortens s to at most max runes, appending "..." when it cuts.
func truncate(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	if max <= 3 {
		return string(r[:max])
	}
	return string(r[:max-3]) + "..."
}
