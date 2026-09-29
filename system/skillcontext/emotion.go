package skillcontext

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/usercanon"
	"go.autonomous.ai/os/system/skillcontext/mood"
	"go.autonomous.ai/os/system/skillcontext/musicsuggestion"
)

// detectedEmotionRe extracts the label from "Emotion detected: Sad." or "Speech emotion detected: Sad.".
var detectedEmotionRe = regexp.MustCompile(`(?i)Emotion detected:\s*([A-Za-z]+)`)

// ExtractDetectedEmotion returns the emotion label from an emotion.detected
// or speech_emotion.detected payload, or "" if it cannot be parsed.
func ExtractDetectedEmotion(message string) string {
	m := detectedEmotionRe.FindStringSubmatch(message)
	if m == nil {
		return ""
	}
	return m[1]
}

const (
	emotionRecentSignalsWindow = 30 * time.Minute
	moodHistoryN               = 50
	musicSuggestionHistoryN    = 5
	decisionStaleAfter         = 30 * time.Minute
	audioStatusTimeout         = 800 * time.Millisecond
	audioHistoryTimeout        = 800 * time.Millisecond
)

// emotionContext is the digest the agent reads on emotion.detected events.
type emotionContext struct {
	MappedMood           string               `json:"mapped_mood"`              // detected emotion → mood signal value (Sad → "sad", Fear → "stressed", ...)
	RecentSignals        []signalDigest       `json:"recent_signals"`           // mood signals in the last emotionRecentSignalsWindow
	PriorDecision        *priorDecisionDigest `json:"prior_decision,omitempty"` // most recent kind=decision row, omitted if none
	IsDecisionStale      bool                 `json:"is_decision_stale"`        // PriorDecision.AgeMin > decisionStaleAfter, or no decision today
	AudioPlaying         bool                 `json:"audio_playing"`            // /audio/status shows playback active
	LastSuggestionAgeMin int                  `json:"last_suggestion_age_min"`  // minutes since last music-suggestion log row; -1 if none today
	AudioRecent          *audioRecentDigest   `json:"audio_recent,omitempty"`   // last entry from /audio/history
	MusicPatternForHour  *musicPatternDigest  `json:"music_pattern_for_hour,omitempty"`
	SuggestionWorthy     bool                 `json:"suggestion_worthy"` // true when MappedMood is in the suggestion-worthy bucket
}

type signalDigest struct {
	AgeMin  int    `json:"age_min"`
	Mood    string `json:"mood"`
	Source  string `json:"source"`
	Trigger string `json:"trigger,omitempty"`
}

type priorDecisionDigest struct {
	Mood   string `json:"mood"`
	AgeMin int    `json:"age_min"`
}

type audioRecentDigest struct {
	Track       string `json:"track,omitempty"`
	DurationS   int    `json:"duration_s,omitempty"`
	StoppedKind string `json:"stopped,omitempty"` // "natural" / "manual" / etc.
}

type musicPatternDigest struct {
	PreferredGenre string `json:"preferred_genre"`
	Strength       string `json:"strength"`
	PeakHour       int    `json:"peak_hour"`
}

// emotionToMood maps face FER and emotion2vec voice labels to mood buckets.
var emotionToMood = map[string]string{
	"happy":     "happy",
	"sad":       "sad",
	"angry":     "frustrated",
	"fear":      "stressed",
	"fearful":   "stressed",
	"surprise":  "excited",
	"surprised": "excited",
	"disgust":   "frustrated",
	"disgusted": "frustrated",
	"neutral":   "normal",
}

var suggestionWorthyMoods = map[string]bool{
	"sad":      true,
	"stressed": true,
	"tired":    true,
	"excited":  true,
	"happy":    true,
	"bored":    true,
}

// BuildEmotionContext returns an `[emotion_context: ...]` block, or "" on hard failure.
func BuildEmotionContext(detectedEmotion, user string) string {
	user = usercanon.Resolve(user)
	if user == "" {
		user = "unknown"
	}
	now := time.Now()
	today := now.Format("2006-01-02")

	mapped := emotionToMood[strings.ToLower(strings.TrimSpace(detectedEmotion))]
	if mapped == "" {
		mapped = "normal"
	}

	moodEvents := mood.Query(user, today, "", moodHistoryN)
	recentSignals := buildRecentSignals(moodEvents, now)
	priorDecision, decisionStale := findPriorDecision(moodEvents, now)

	suggestionEvents := musicsuggestion.Query(user, today, musicSuggestionHistoryN)
	lastSuggestionAge := lastSuggestionAgeMin(suggestionEvents, now)

	audioPlaying := fetchAudioPlaying()
	audioRecent := fetchAudioRecent(user)

	musicPattern := matchMusicPatternForHour(readPatternsRaw(user), now.Hour())

	ctx := emotionContext{
		MappedMood:           mapped,
		RecentSignals:        recentSignals,
		PriorDecision:        priorDecision,
		IsDecisionStale:      decisionStale,
		AudioPlaying:         audioPlaying,
		LastSuggestionAgeMin: lastSuggestionAge,
		AudioRecent:          audioRecent,
		MusicPatternForHour:  musicPattern,
		SuggestionWorthy:     suggestionWorthyMoods[mapped],
	}

	body, err := json.Marshal(ctx)
	if err != nil {
		slog.Warn("emotion context: marshal failed", "component", "skillcontext", "error", err)
		return ""
	}
	return fmt.Sprintf("\n[emotion_context: %s]", string(body))
}

func buildRecentSignals(events []mood.Event, now time.Time) []signalDigest {
	cutoff := now.Add(-emotionRecentSignalsWindow)
	out := make([]signalDigest, 0, 8)
	for _, e := range events {
		if e.Kind != mood.KindSignal {
			continue
		}
		ts := time.Unix(int64(e.TS), 0)
		if ts.Before(cutoff) {
			continue
		}
		out = append(out, signalDigest{
			AgeMin:  int(now.Sub(ts).Minutes()),
			Mood:    e.Mood,
			Source:  e.Source,
			Trigger: e.Trigger,
		})
	}
	return out
}

func findPriorDecision(events []mood.Event, now time.Time) (*priorDecisionDigest, bool) {
	for i := len(events) - 1; i >= 0; i-- {
		e := events[i]
		if e.Kind != mood.KindDecision {
			continue
		}
		ageMin := int(now.Sub(time.Unix(int64(e.TS), 0)).Minutes())
		stale := time.Duration(ageMin)*time.Minute >= decisionStaleAfter
		return &priorDecisionDigest{Mood: e.Mood, AgeMin: ageMin}, stale
	}
	return nil, true // no decision today → treat as stale
}

func lastSuggestionAgeMin(events []musicsuggestion.Event, now time.Time) int {
	if len(events) == 0 {
		return -1
	}
	last := events[len(events)-1]
	return int(now.Sub(time.Unix(int64(last.TS), 0)).Minutes())
}

// fetchAudioPlaying reports whether HAL is playing audio; false on any error.
func fetchAudioPlaying() bool {
	client := &http.Client{Timeout: audioStatusTimeout}
	resp, err := client.Get(hal.BaseURL + "/audio/status")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return false
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil {
		return false
	}
	var payload struct {
		Playing bool `json:"playing"`
	}
	if json.Unmarshal(body, &payload) != nil {
		return false
	}
	return payload.Playing
}

// fetchAudioRecent returns HAL's latest global play (HAL does not tag plays by person).
func fetchAudioRecent(_ string) *audioRecentDigest {
	client := &http.Client{Timeout: audioHistoryTimeout}
	url := hal.BaseURL + "/audio/history?last=1"
	resp, err := client.Get(url)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8192))
	if err != nil {
		return nil
	}
	var payload struct {
		Entries []struct {
			Title     string  `json:"title"`
			Duration  float64 `json:"duration_s"`
			StoppedBy string  `json:"stopped_by"`
		} `json:"entries"`
	}
	if json.Unmarshal(body, &payload) != nil || len(payload.Entries) == 0 {
		return nil
	}
	last := payload.Entries[len(payload.Entries)-1]
	if last.Title == "" {
		return nil
	}
	return &audioRecentDigest{
		Track:       last.Title,
		DurationS:   int(last.Duration),
		StoppedKind: last.StoppedBy,
	}
}

// readPatternsRaw returns patterns.json if fresh (patternsFreshAge), else nil.
func readPatternsRaw(user string) []byte {
	path := filepath.Join(usersDir, user, patternsSubpath)
	info, err := os.Stat(path)
	if err != nil {
		return nil
	}
	if time.Since(info.ModTime()) >= patternsFreshAge {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	return data
}

// matchMusicPatternForHour returns a moderate+ music pattern with peak_hour within +/-1, or nil.
func matchMusicPatternForHour(patternsRaw []byte, hour int) *musicPatternDigest {
	if len(patternsRaw) == 0 {
		return nil
	}
	var raw struct {
		MusicPatterns []struct {
			PeakHour       int    `json:"peak_hour"`
			PreferredGenre string `json:"preferred_genre"`
			Strength       string `json:"strength"`
		} `json:"music_patterns"`
	}
	if json.Unmarshal(patternsRaw, &raw) != nil {
		return nil
	}
	for _, p := range raw.MusicPatterns {
		if p.Strength != "moderate" && p.Strength != "strong" {
			continue
		}
		if abs(p.PeakHour-hour) <= 1 {
			return &musicPatternDigest{
				PreferredGenre: p.PreferredGenre,
				Strength:       p.Strength,
				PeakHour:       p.PeakHour,
			}
		}
	}
	return nil
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}
