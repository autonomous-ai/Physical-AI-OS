// Package hal is a lightweight HTTP client for the HAL hardware API on port 5001.
package hal

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"
)

// ErrSpeakerMuted reports that HAL accepted a speak request but suppressed it (speaker muted).
var ErrSpeakerMuted = errors.New("speaker muted")

const BaseURL = "http://127.0.0.1:5001"

var httpClient = &http.Client{Timeout: 5 * time.Second}

// apiKey is the HAL bearer token (config llm_api_key); the header is sent only when set.
var apiKey atomic.Value // string

// SetAPIKey sets the bearer token for outbound requests; "" drops the header.
func SetAPIKey(key string) {
	apiKey.Store(key)
}

func getAPIKey() string {
	if v := apiKey.Load(); v != nil {
		return v.(string)
	}
	return ""
}

// newRequest builds a request to BaseURL+path with JSON content type and bearer auth when set.
func newRequest(method, path string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, BaseURL+path, body)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if k := getAPIKey(); k != "" {
		req.Header.Set("Authorization", "Bearer "+k)
	}
	return req, nil
}

// doGet sends an authorized GET to HAL.
func doGet(path string) (*http.Response, error) {
	req, err := newRequest("GET", path, nil)
	if err != nil {
		return nil, err
	}
	return httpClient.Do(req)
}

func doPost(path string, body io.Reader) (*http.Response, error) {
	req, err := newRequest("POST", path, body)
	if err != nil {
		return nil, err
	}
	return httpClient.Do(req)
}

// snapshotClient has a longer timeout: /camera/snapshot freezes servos and waits for a stable frame.
var snapshotClient = &http.Client{Timeout: 20 * time.Second}

// Snapshot captures a frame and returns the file path HAL saved it to.
func Snapshot(width, quality int) (string, error) {
	req, err := newRequest("GET", fmt.Sprintf("/camera/snapshot?save=true&width=%d&quality=%d", width, quality), nil)
	if err != nil {
		return "", err
	}
	resp, err := snapshotClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Surface HAL's `detail`; the agent uses it to decide whether to retry.
		var body struct {
			Detail string `json:"detail"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&body)
		if body.Detail != "" {
			return "", fmt.Errorf("GET /camera/snapshot returned %d: %s", resp.StatusCode, body.Detail)
		}
		return "", fmt.Errorf("GET /camera/snapshot returned %d", resp.StatusCode)
	}
	var result struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decode /camera/snapshot: %w", err)
	}
	if result.Path == "" {
		return "", errors.New("/camera/snapshot returned no path")
	}
	return result.Path, nil
}

// SetEffect replaces any running effect with a transient one that never overwrites the user's saved LED state.
func SetEffect(effect string, r, g, b int, speed float64) {
	postSilent("/led/effect/stop", "{}")
	body := fmt.Sprintf(`{"effect":"%s","color":[%d,%d,%d],"speed":%.2f,"transient":true}`, effect, r, g, b, speed)
	postSilent("/led/effect", body)
}

// StopEffect stops any running LED effect.
func StopEffect() {
	postSilent("/led/effect/stop", "{}")
}

// SetStatus applies a named status LED state (HAL owns its look) as a transient overlay; fire-and-forget.
// Example: SetStatus("booting")
func SetStatus(stateName string) {
	postSilent("/led/status", fmt.Sprintf(`{"state":%q}`, stateName))
}

// SetStatusContext checks HAL's acknowledgement and supports shutdown cancellation.
func SetStatusContext(ctx context.Context, stateName string) error {
	req, err := newRequest(http.MethodPost, "/led/status", strings.NewReader(fmt.Sprintf(`{"state":%q}`, stateName)))
	if err != nil {
		return err
	}
	resp, err := httpClient.Do(req.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("POST /led/status: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("POST /led/status returned %d", resp.StatusCode)
	}
	var result struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return fmt.Errorf("decode /led/status: %w", err)
	}
	if result.Status != "ok" {
		return fmt.Errorf("POST /led/status not acknowledged: %q", result.Status)
	}
	return nil
}

// RestoreLED returns the strip to the user's saved LED state after a transient overlay.
func RestoreLED() {
	postSilent("/led/restore", "{}")
}

// ResetLEDToResting clears the saved user LED state and turns the strip off.
func ResetLEDToResting() {
	postSilent("/led/off", "{}")
}

// GetColor returns the current LED color as [R, G, B].
func GetColor() ([3]int, error) {
	resp, err := doGet("/led/color")
	if err != nil {
		return [3]int{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return [3]int{}, fmt.Errorf("GET /led/color returned %d", resp.StatusCode)
	}
	var result struct {
		Color []*int `json:"color"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return [3]int{}, fmt.Errorf("decode /led/color: %w", err)
	}
	if len(result.Color) != 3 {
		return [3]int{}, errors.New("/led/color returned missing or invalid color")
	}
	var color [3]int
	for i, channel := range result.Color {
		if channel == nil || *channel < 0 || *channel > 255 {
			return [3]int{}, errors.New("/led/color returned invalid color channel")
		}
		color[i] = *channel
	}
	return color, nil
}

// Speak sends text to TTS playback.
func Speak(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/speak", body)
}

// GrantWakeFocus opens HAL's wake-word follow-up window so the next utterance
// dispatches without a wake phrase. HAL no-ops when wake word is off.
func GrantWakeFocus(source string) error {
	return post("/voice/wake-focus?source="+url.QueryEscape(source), nil)
}

// ApplyTTSConfig pushes voice settings into the running HAL; applies from the next sentence.
func ApplyTTSConfig(provider, voice, apiKey, baseURL string, speed float64) error {
	body, _ := json.Marshal(map[string]any{
		"speed":    speed,
		"provider": provider,
		"voice":    voice,
		"api_key":  apiKey,
		"base_url": baseURL,
	})
	return post("/voice/tts/config", body)
}

// SpeakQueue speaks text, queueing and pre-synthesizing it behind current playback for gapless replies.
func SpeakQueue(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/speak-queue", body)
}

// SpeakReply speaks an agent reply and feeds it to the realtime voice agent's history.
// Use only for genuine agent output, never hardcoded phrases.
func SpeakReply(text string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "realtime_feedback": true})
	return postSpeak("/voice/speak", body)
}

// FeedRealtimeHistory records an agent reply with the realtime voice agent without speaking it.
// Genuine agent output only, as with SpeakReply.
func FeedRealtimeHistory(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/realtime/history", body)
}

// SpeakQueueReply is SpeakQueue with realtime feedback (see SpeakReply).
func SpeakQueueReply(text string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "realtime_feedback": true})
	return postSpeak("/voice/speak-queue", body)
}

// SpeakQueueReplyForTurn is SpeakQueueReply owned by a turn; HAL lets the highest turnSeq win.
func SpeakQueueReplyForTurn(text, turnID string, turnSeq uint64) error {
	body, _ := json.Marshal(map[string]any{
		"text":              text,
		"realtime_feedback": true,
		"turn_id":           turnID,
		"turn_seq":          turnSeq,
	})
	return postSpeak("/voice/speak-queue", body)
}

// SpeakInterruptible sends text to TTS; playback can be cut short by incoming voice.
func SpeakInterruptible(text string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "interruptible": true})
	return post("/voice/speak", body)
}

// SpeakCached plays text via the WAV cache, non-interruptible. Example: SpeakCached("Light on!")
func SpeakCached(text string) error {
	return SpeakCachedForTurn(text, "")
}

// SpeakCachedForTurn is SpeakCached with turnID for metrics attribution only.
func SpeakCachedForTurn(text, turnID string) error {
	payload := map[string]any{
		"text":   text,
		"cached": true,
	}
	if turnID != "" {
		payload["turn_id"] = turnID
	}
	body, _ := json.Marshal(payload)
	return post("/voice/speak", body)
}

// SpeakCachedInterruptible plays text via the WAV cache; a real reply may cut it short.
func SpeakCachedInterruptible(text string) error {
	return SpeakCachedInterruptibleForTurn(text, "")
}

// SpeakCachedInterruptibleForTurn is SpeakCachedInterruptible with turnID for metrics attribution only.
func SpeakCachedInterruptibleForTurn(text, turnID string) error {
	payload := map[string]any{
		"text":          text,
		"interruptible": true,
		"cached":        true,
	}
	if turnID != "" {
		payload["turn_id"] = turnID
	}
	body, _ := json.Marshal(payload)
	return post("/voice/speak", body)
}

// SpeakPreview plays a TTS preview; empty args use HAL defaults, optional speed applies to this preview only.
func SpeakPreview(text, voice, provider, apiKey, baseURL string, speed ...*float64) error {
	payload := map[string]any{"text": text}
	if len(speed) > 0 && speed[0] != nil {
		payload["speed"] = *speed[0]
	}
	if voice != "" {
		payload["voice"] = voice
	}
	if provider != "" {
		payload["provider"] = provider
	}
	if apiKey != "" {
		payload["tts_api_key"] = apiKey
	}
	if baseURL != "" {
		payload["tts_base_url"] = baseURL
	}
	body, _ := json.Marshal(payload)
	// First synthesis can exceed the default 5s budget.
	return postWithTimeout("/voice/speak", body, 30*time.Second)
}

// PrerenderCached renders and caches the WAV for text without playing it; idempotent.
func PrerenderCached(text string) error {
	body, _ := json.Marshal(map[string]any{
		"text":      text,
		"cached":    true,
		"prerender": true,
	})
	return postWithTimeout("/voice/speak", body, 30*time.Second)
}

// StopTTS interrupts active TTS playback.
func StopTTS() error { return post("/tts/stop", nil) }

// StopAudio stops any audio playback (music, etc.).
func StopAudio() error { return post("/audio/stop", nil) }

// RebootOS requests HAL's full reboot action (cue, then OS reboot).
func RebootOS() error { return post("/system/reboot", nil) }

// ShutdownOS requests HAL's full shutdown action (cue, servo release, then OS shutdown).
func ShutdownOS() error { return post("/system/shutdown", nil) }

// SetVolume sets speaker volume (0-100).
func SetVolume(pct int) error {
	body, _ := json.Marshal(map[string]int{"volume": pct})
	return post("/audio/volume", body)
}

// GetVolume returns the current volume and ceiling (100 when HAL reports none).
// An invalid current volume is an error so callers never turn a quiet speaker up.
func GetVolume() (current, ceiling int, err error) {
	resp, err := doGet("/audio/volume")
	if err != nil {
		return 0, 0, fmt.Errorf("GET /audio/volume: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return 0, 0, fmt.Errorf("GET /audio/volume returned %d", resp.StatusCode)
	}
	var result struct {
		Volume    *int `json:"volume"`
		MaxVolume *int `json:"max_volume"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return 0, 0, fmt.Errorf("decode /audio/volume: %w", err)
	}
	if result.Volume == nil || *result.Volume < 0 || *result.Volume > 100 {
		return 0, 0, errors.New("/audio/volume returned missing or invalid volume")
	}
	ceiling = 100
	if result.MaxVolume != nil {
		ceiling = *result.MaxVolume
		if ceiling < 0 || ceiling > 100 {
			return 0, 0, errors.New("/audio/volume returned invalid max_volume")
		}
	}
	return *result.Volume, ceiling, nil
}

// MaxVolume returns the SAFETY.md speaker ceiling (%), false when none is declared.
// Advisory only: HAL clamps every /audio/volume request regardless.
func MaxVolume() (int, bool) {
	resp, err := doGet("/audio/volume")
	if err != nil {
		return 0, false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return 0, false
	}
	var r struct {
		MaxVolume *int `json:"max_volume"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil || r.MaxVolume == nil {
		return 0, false
	}
	return *r.MaxVolume, true
}

// VoiceStartConfig configures StartVoice; empty optional fields are omitted and STTKey/TTSKey fall back to LLMKey.
type VoiceStartConfig struct {
	DeepgramKey     string
	LLMKey          string
	STTKey          string
	TTSKey          string
	LLMBaseURL      string
	STTBaseURL      string
	TTSBaseURL      string
	TTSVoice        string
	TTSInstructions string
	TTSProvider     string
}

// StartVoice starts the voice pipeline with the given config.
func StartVoice(cfg VoiceStartConfig) error {
	payload := map[string]string{
		"deepgram_api_key": cfg.DeepgramKey,
		"llm_api_key":      cfg.LLMKey,
		"llm_base_url":     cfg.LLMBaseURL,
	}
	if cfg.STTKey != "" {
		payload["stt_api_key"] = cfg.STTKey
	}
	if cfg.TTSKey != "" {
		payload["tts_api_key"] = cfg.TTSKey
	}
	if cfg.STTBaseURL != "" {
		payload["stt_base_url"] = cfg.STTBaseURL
	}
	if cfg.TTSBaseURL != "" {
		payload["tts_base_url"] = cfg.TTSBaseURL
	}
	if cfg.TTSVoice != "" {
		payload["tts_voice"] = cfg.TTSVoice
	}
	if cfg.TTSInstructions != "" {
		payload["tts_instructions"] = cfg.TTSInstructions
	}
	if cfg.TTSProvider != "" {
		payload["tts_provider"] = cfg.TTSProvider
	}
	body, _ := json.Marshal(payload)
	return post("/voice/start", body)
}

// StopVoicePipeline stops the whole voice pipeline (StopTTS only interrupts playback).
func StopVoicePipeline() error {
	return post("/voice/stop", []byte("{}"))
}

// ListVoices returns TTS voices for provider, filtered by BCP-47 lang when non-empty.
func ListVoices(provider, lang string) ([]string, error) {
	path := "/voice/voices?provider=" + provider
	if lang != "" {
		path += "&lang=" + lang
	}
	resp, err := doGet(path)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("GET /voice/voices returned %d", resp.StatusCode)
	}
	var result struct {
		Provider string   `json:"provider"`
		Voices   []string `json:"voices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, fmt.Errorf("decode /voice/voices: %w", err)
	}
	return result.Voices, nil
}

// SetVoiceConfig updates the voice pipeline config at runtime (e.g. wake words after rename).
func SetVoiceConfig(wakeWords []string) {
	b, err := json.Marshal(map[string]any{"wake_words": wakeWords})
	if err != nil {
		return
	}
	postSilent("/voice/config", string(b))
}

// Health mirrors the /health response from HAL.
type Health struct {
	Servo   bool `json:"servo"`
	LED     bool `json:"led"`
	Camera  bool `json:"camera"`
	Audio   bool `json:"audio"`
	Sensing bool `json:"sensing"`
	Voice   bool `json:"voice"`
	TTS     bool `json:"tts"`
}

// GetVersion returns HAL's runtime version from /version.
func GetVersion() (string, error) {
	resp, err := doGet("/version")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("GET /version returned %d", resp.StatusCode)
	}
	var r struct {
		Version string `json:"version"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", fmt.Errorf("decode /version: %w", err)
	}
	return r.Version, nil
}

// GetHealth returns the current health snapshot from HAL.
func GetHealth() (*Health, error) {
	return GetHealthContext(context.Background())
}

// GetHealthContext allows background readiness checks to stop during shutdown.
func GetHealthContext(ctx context.Context) (*Health, error) {
	req, err := newRequest(http.MethodGet, "/health", nil)
	if err != nil {
		return nil, err
	}
	resp, err := httpClient.Do(req.WithContext(ctx))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("GET /health returned %d", resp.StatusCode)
	}
	var h Health
	if err := json.NewDecoder(resp.Body).Decode(&h); err != nil {
		return nil, fmt.Errorf("decode /health: %w", err)
	}
	return &h, nil
}

// ServoInfo is a single servo in ServoStatus.
type ServoInfo struct {
	ID     int     `json:"id"`
	Angle  float64 `json:"angle"`
	Online bool    `json:"online"`
	Error  *string `json:"error"`
}

// ServoStatus is the /servo/status response.
type ServoStatus struct {
	Servos map[string]ServoInfo `json:"servos"`
}

// GetServoStatus returns per-servo online state.
func GetServoStatus() (*ServoStatus, error) {
	resp, err := doGet("/servo/status")
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("GET /servo/status returned %d", resp.StatusCode)
	}
	var ss ServoStatus
	if err := json.NewDecoder(resp.Body).Decode(&ss); err != nil {
		return nil, fmt.Errorf("decode /servo/status: %w", err)
	}
	return &ss, nil
}

// PlayServo plays a named servo recording.
func PlayServo(recording string) error {
	body, _ := json.Marshal(map[string]string{"recording": recording})
	return post("/servo/play", body)
}

// StopServoTracking halts servo object-tracking; idempotent. Called when the gateway link drops
// so the device never chases a stale target.
func StopServoTracking() error { return post("/servo/track/stop", nil) }

// SetEmotion triggers an emotion animation on HAL.
func SetEmotion(name string, intensity float64) error {
	body, _ := json.Marshal(map[string]any{"emotion": name, "intensity": intensity})
	return post("/emotion", body)
}

// GetSleeping returns HAL's authoritative sleep flag from /emotion/status.
func GetSleeping() (bool, error) {
	resp, err := doGet("/emotion/status")
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, fmt.Errorf("GET /emotion/status returned %d", resp.StatusCode)
	}
	var r struct {
		Sleeping *bool `json:"sleeping"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return false, fmt.Errorf("decode /emotion/status: %w", err)
	}
	if r.Sleeping == nil {
		return false, errors.New("/emotion/status returned missing sleeping state")
	}
	return *r.Sleeping, nil
}

// GetEmotion returns the current emotion reported by HAL's /emotion/status.
func GetEmotion() (string, error) {
	resp, err := doGet("/emotion/status")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("GET /emotion/status returned %d", resp.StatusCode)
	}
	var r struct {
		CurrentEmotion string `json:"current_emotion"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", fmt.Errorf("decode /emotion/status: %w", err)
	}
	return r.CurrentEmotion, nil
}

// PostRaw sends a JSON body to a dynamic path; an empty body sends none.
func PostRaw(path, body string) error {
	if body == "" {
		return post(path, nil)
	}
	return post(path, []byte(body))
}

// post sends a JSON body and returns an error on transport failure or non-2xx status.
func post(path string, body []byte) error {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	resp, err := doPost(path, reader)
	if err != nil {
		return fmt.Errorf("POST %s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("POST %s returned %d", path, resp.StatusCode)
	}
	return nil
}

// postSpeak is post for /voice/speak*, returning ErrSpeakerMuted when HAL suppressed the speech.
func postSpeak(path string, body []byte) (err error) {
	var timing struct {
		Text   string `json:"text"`
		TurnID string `json:"turn_id"`
	}
	if json.Unmarshal(body, &timing) == nil && timing.Text != "" {
		started := time.Now()
		digest := sha256.Sum256([]byte(timing.Text))
		textKey := fmt.Sprintf("%x", digest[:6])
		slog.Info("[tts-timing] hal_post_start", "path", path, "run_id", timing.TurnID, "text_key", textKey)
		defer func() {
			slog.Info("[tts-timing] hal_post_complete", "path", path, "run_id", timing.TurnID,
				"text_key", textKey, "http_ms", time.Since(started).Milliseconds(), "success", err == nil)
		}()
	}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	resp, err := doPost(path, reader)
	if err != nil {
		return fmt.Errorf("POST %s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("POST %s returned %d", path, resp.StatusCode)
	}
	var r struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err == nil && r.Status == "suppressed" {
		return ErrSpeakerMuted
	}
	return nil
}

// postWithTimeout is post with a per-call timeout for slow endpoints.
func postWithTimeout(path string, body []byte, timeout time.Duration) error {
	client := &http.Client{Timeout: timeout}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := newRequest("POST", path, reader)
	if err != nil {
		return fmt.Errorf("POST %s: %w", path, err)
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("POST %s: %w", path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("POST %s returned %d", path, resp.StatusCode)
	}
	return nil
}

// postSilent is a fire-and-forget post; errors are ignored.
func postSilent(path, body string) {
	resp, err := doPost(path, strings.NewReader(body))
	if err != nil {
		return
	}
	resp.Body.Close()
}

// SpeakerBusy reports whether HAL is speaking; fails open (errors report not busy).
func SpeakerBusy() bool {
	resp, err := doGet("/voice/status")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false
	}
	var r struct {
		TTSSpeaking bool `json:"tts_speaking"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return false
	}
	return r.TTSSpeaking
}
