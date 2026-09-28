// Package hal provides a lightweight HTTP client for the HAL hardware API.
// Both os-server and bootstrap-server use this to control the device on port 5001.
package hal

import (
	"bytes"
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

// ErrSpeakerMuted reports that HAL accepted the speak request but suppressed
// playback because the device speaker is muted (HTTP 200 with
// status="suppressed" — see HAL routes/voice.py). Nothing was synthesized or
// spoken. Callers can surface the mute (e.g. a tts_muted flow event) instead
// of treating it as a delivery failure.
var ErrSpeakerMuted = errors.New("speaker muted")

const BaseURL = "http://127.0.0.1:5001"

var httpClient = &http.Client{Timeout: 5 * time.Second}

// apiKey is the shared HAL auth token (matches config.json::llm_api_key).
// HAL's local_only_middleware accepts Authorization: Bearer <apiKey> as one
// of the allowed paths. Loopback callers still pass without a token, so an
// unset key keeps existing behavior — the header is only attached when set.
// atomic.Value lets the server's config-change listener swap the key at runtime
// without a mutex on the hot request path.
var apiKey atomic.Value // string

// SetAPIKey registers the bearer token attached to every outbound request.
// Pass the empty string to drop the Authorization header (e.g. local LLM
// mode where llm_api_key is unset).
func SetAPIKey(key string) {
	apiKey.Store(key)
}

func getAPIKey() string {
	if v := apiKey.Load(); v != nil {
		return v.(string)
	}
	return ""
}

// newRequest builds an http.Request to BaseURL+path with JSON content type
// (when a body is present) and the bearer Authorization header (when set).
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

// doGet / doPost are thin wrappers so every call site picks up the
// Authorization header automatically. Replace direct httpClient.Get/Post.
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

// ─── Camera ─────────────────────────────────────────────────────────────────

// snapshotClient is separate from httpClient because /camera/snapshot freezes
// the servos and waits for a stable frame, which overruns the shared 5s budget.
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
		// Carry HAL's `detail` up: the agent reads this string to decide whether
		// to retry, and "returned 503" alone reads as a hiccup while HAL may be
		// saying the camera hardware is absent.
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

// ─── LED ────────────────────────────────────────────────────────────────────

// SetEffect stops any running effect, then starts a new one.
//
// All callers from the os-server (statusled health signals, ambient breathing, bootstrap
// OTA progress) are system-level overlays — they must not clobber the user's
// saved LED state, which emotion restore reads back from. The transient flag
// tells HAL to dispatch the effect without writing _user_led_state.
func SetEffect(effect string, r, g, b int, speed float64) {
	postSilent("/led/effect/stop", "{}")
	body := fmt.Sprintf(`{"effect":"%s","color":[%d,%d,%d],"speed":%.2f,"transient":true}`, effect, r, g, b, speed)
	postSilent("/led/effect", body)
}

// StopEffect stops any running LED effect.
func StopEffect() {
	postSilent("/led/effect/stop", "{}")
}

// SetStatus applies an os-server status state (booting/error/ota/…) by name.
// HAL owns the appearance (color/effect/speed) for each state via STATUS_LED_PRESETS
// — overridable per device in presets.json — so the OS sends only the state name,
// never an RGB. Like SetEffect this is a transient system overlay (HAL applies it
// without clobbering the user's saved LED state). Fire-and-forget.
func SetStatus(stateName string) {
	postSilent("/led/status", fmt.Sprintf(`{"state":%q}`, stateName))
}

// RestoreLED hands the strip back to the user's saved LED state (or clears it
// when no user state exists). Use after a transient overlay (statusled flash,
// OTA progress) finishes so the strip doesn't get stuck on the overlay's
// final frame.
func RestoreLED() {
	postSilent("/led/restore", "{}")
}

// ResetLEDToResting clears HAL's saved user LED state and turns the strip off.
// The next restore therefore uses the configured ambient resting look. This is
// used when setup succeeds to discard the temporary white provisioning cue.
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

// ─── Voice / TTS ────────────────────────────────────────────────────────────

// Speak sends text to TTS playback (speaker locks mic during playback).
func Speak(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/speak", body)
}

// GrantWakeFocus opens HAL's wake-word follow-up window so the next utterance
// dispatches without a wake phrase. HAL no-ops when wake word is off.
func GrantWakeFocus(source string) error {
	return post("/voice/wake-focus?source="+url.QueryEscape(source), nil)
}

// ApplyTTSConfig pushes voice settings into the running hal. The service reads
// provider and voice per utterance, so this takes effect on the next sentence
// — where the restart it replaces took the microphone, speaker and wake word
// down with it for ten to fifteen seconds.
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

// SpeakQueue sends text to /voice/speak-queue — same playback semantics as
// Speak when the speaker is idle, but queues+pre-synthesizes the audio if
// the speaker is currently playing another speak(). The queued audio
// continues on the same ALSA stream when the current speech ends, so the
// agent's sentence-streamed reply plays as one continuous utterance instead
// of N speak() calls separated by ~400ms TTFB each.
func SpeakQueue(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/speak-queue", body)
}

// SpeakReply is Speak for the agentic runtime's actual reply: it sets
// realtime_feedback so HAL feeds the spoken text back to the realtime voice
// agent as [TTS HISTORY] (keeping it aware of what the device said). Use this
// ONLY for genuine agent output — hardcoded TTS (fillers, mumble, system
// notices) must use plain Speak so it never pollutes the realtime model.
func SpeakReply(text string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "realtime_feedback": true})
	return postSpeak("/voice/speak", body)
}

// FeedRealtimeHistory records an agent reply with the realtime voice agent
// WITHOUT speaking it. Needed for a reply the speaker never gets: a turn muted
// by the physical cancel gesture keeps running and its text is still the
// answer the user asked for, but the normal history feed rides on TTS
// completion, so dropping the speech also dropped the realtime session's only
// record of the answer. Same rule as SpeakReply about what may be sent —
// genuine agent output only, never hardcoded notices.
func FeedRealtimeHistory(text string) error {
	body, _ := json.Marshal(map[string]string{"text": text})
	return post("/voice/realtime/history", body)
}

// SpeakQueueReply is SpeakQueue with realtime feedback — the queued sibling of
// SpeakReply for sentence-streamed agent replies. See SpeakReply.
func SpeakQueueReply(text string) error {
	body, _ := json.Marshal(map[string]any{"text": text, "realtime_feedback": true})
	return postSpeak("/voice/speak-queue", body)
}

// SpeakQueueReplyForTurn is the turn-owned form of SpeakQueueReply. turnSeq
// is allocated by the agent handler when a turn starts. HAL uses the pair to
// make the newest turn win even when background HTTP posts arrive out of order.
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

// SpeakCached is the non-interruptible cached variant -- used for intent
// confirms ("Volume up!", "Light on!") where the reply is short and should
// play to completion. On hit ~50ms playback; on miss render+save+play.
func SpeakCached(text string) error {
	return SpeakCachedForTurn(text, "")
}

// SpeakCachedForTurn is SpeakCached plus the turn (or voice-metrics
// interaction) the phrase answers. HAL uses turnID for measurement
// attribution only; playback behaviour is identical.
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

// SpeakCachedInterruptible plays text via the WAV cache (instant on hit).
// On miss, hal renders + saves WAV then plays. Use for fixed phrases
// like dead-air fillers where a real reply may need to cut it short.
func SpeakCachedInterruptible(text string) error {
	return SpeakCachedInterruptibleForTurn(text, "")
}

// SpeakCachedInterruptibleForTurn is SpeakCachedInterruptible plus the run the
// phrase belongs to. HAL uses turnID for measurement attribution only (which
// turn a played filler was for — see hal/telemetry/voice_metrics.py); playback
// behaviour is identical, and the turn_seq gating stays exclusive to the
// speak-queue path.
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

// SpeakPreview plays a TTS preview using the supplied voice/provider/credentials.
// The os-server's /api/voice/preview handler uses this to fan out the operator's
// "test voice" click without exposing the TTS API key in the browser body —
// the os-server reads the key server-side from config and passes it here. Each arg
// can be empty: HAL falls back to its own config-loaded defaults when a
// field is missing, so partial overrides (e.g. just voice) work.
// An optional speed overrides the saved rate for this preview only.
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
	// Generous timeout: ElevenLabs/OpenAI TTFB on first synthesis can run
	// 1-3s; the default 5s `post` budget is tight when the preview phrase
	// is long. Mirror PrerenderCached's window.
	return postWithTimeout("/voice/speak", body, 30*time.Second)
}

// PrerenderCached asks hal to render+save WAV for text without playing.
// Used at startup to warm the cache for known fillers/intent confirms so
// the first runtime call is a hit. Idempotent: no-op when WAV already exists.
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

// GetVolume reads the current speaker volume and its allowed ceiling together.
// A missing ceiling is compatible with older HAL versions; a missing or invalid
// current volume must fail rather than accidentally turn a quiet speaker up.
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

// MaxVolume returns the speaker ceiling (%) HAL enforces from the device's
// SAFETY.md `audio.max_volume`, and true when one is declared. False means the
// device declares no ceiling — the full 0-100 scale is available.
//
// This is advisory only: HAL clamps every /audio/volume request regardless, so a
// caller that skips this (or whose read fails) cannot exceed the bound — it just
// loses the ability to scale its steps to the real range.
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

// VoiceStartConfig configures the voice pipeline started by StartVoice.
// Empty TTSInstructions and TTSProvider are omitted from the payload.
//
// LLMKey authenticates LLM-based features. STTKey authenticates
// AutonomousSTT (used when DeepgramKey is empty); TTSKey authenticates
// the TTS provider. Empty STTKey/TTSKey means HAL falls back to
// LLMKey — keep them empty when one credential covers everything.
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

// StopVoicePipeline stops the voice pipeline entirely (different from StopTTS
// which only interrupts active playback). Used by healthwatch to clear a stuck
// ALSA stream before restarting.
func StopVoicePipeline() error {
	return post("/voice/stop", []byte("{}"))
}

// ListVoices returns available TTS voices for the given provider, filtered
// to lang's curated bucket when lang is non-empty (BCP-47, e.g. "vi",
// "zh-CN"). Empty lang returns the full flat list. Returns an error if
// HAL is unreachable or returns non-2xx — callers should fall back to
// a static list in that case.
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

// ─── Health / Servo ─────────────────────────────────────────────────────────

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

// GetVersion returns HAL's runtime version string (from FastAPI app.version
// at /version). Empty + error if HAL is unreachable.
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
	resp, err := doGet("/health")
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

// StopServoTracking halts any in-flight servo object-tracking on HAL. Used as a
// safety reflex when the gateway link drops: tracking is fed by remote vision
// updates, so without the cloud the device would keep chasing a stale target.
// Idempotent — safe to call when nothing is tracking.
func StopServoTracking() error { return post("/servo/track/stop", nil) }

// ─── Emotion ────────────────────────────────────────────────────────────────

// SetEmotion triggers an emotion animation on HAL.
func SetEmotion(name string, intensity float64) error {
	body, _ := json.Marshal(map[string]any{"emotion": name, "intensity": intensity})
	return post("/emotion", body)
}

// GetSleeping returns HAL's own sleep flag from /emotion/status. HAL updates it
// on every /emotion call, including the ones that never reach os-server (a
// button tap wakes the device in-process), so it is the authoritative answer.
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

// ─── Generic passthrough ────────────────────────────────────────────────────

// PostRaw sends a JSON body to the given path. Use when the path is dynamic
// (e.g. HW markers emitted by the agent or local intent rules). Empty body
// sends nil request body.
func PostRaw(path, body string) error {
	if body == "" {
		return post(path, nil)
	}
	return post(path, []byte(body))
}

// ─── Internals ──────────────────────────────────────────────────────────────

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

// postSpeak is post() for /voice/speak* endpoints: on 2xx it also decodes the
// response body and returns ErrSpeakerMuted when HAL reports the request was
// suppressed (speaker muted). A malformed/unexpected body is NOT an error —
// the speak itself succeeded, so decode failures are ignored.
func postSpeak(path string, body []byte) (err error) {
	// Observe the final runtime-sanitized payload without adding wire fields.
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

// postWithTimeout is post() with a per-call timeout override -- needed for
// long-running endpoints like /voice/speak prerender that can take 1-3s
// per ElevenLabs render and would exceed the default httpClient 5s budget
// when warming many phrases serially.
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

// postSilent is a fire-and-forget variant for LED calls — hardware may be
// unavailable (e.g. during boot) and callers don't care about the outcome.
func postSilent(path, body string) {
	resp, err := doPost(path, strings.NewReader(body))
	if err != nil {
		return
	}
	resp.Body.Close()
}

// SpeakerBusy reports whether HAL is currently speaking a TTS utterance.
//
// The agent turn that produced a reply ends when the text is handed to the TTS
// queue, not when the speaker finishes with it — a long answer keeps playing
// for tens of seconds after that. Callers that must not step on a reply in
// progress (sensing replay, see lib/speakergate) ask here instead of trusting
// the agent's busy flag.
//
// Fails open: any transport or decode error reports "not busy", so a HAL that
// is down or slow can never wedge the sensing pipeline.
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
