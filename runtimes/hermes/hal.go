package hermes

import (
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
)

// stripForTTS regexes — package-level, compiled once.
var (
	reEmoji      = regexp.MustCompile(`[\x{1F300}-\x{1F9FF}\x{2600}-\x{27BF}\x{FE00}-\x{FE0F}\x{200D}\x{20E3}\x{E0020}-\x{E007F}]`)
	reMDBold     = regexp.MustCompile(`\*{1,3}([^*]+)\*{1,3}`)
	reMDItalic   = regexp.MustCompile(`_{1,3}([^_]+)_{1,3}`)
	reMDLink     = regexp.MustCompile(`\[([^\]]+)\]\([^)]+\)`)
	reCodeBlock  = regexp.MustCompile("```[\\s\\S]*?```")
	reInlineCode = regexp.MustCompile("`([^`]+)`")
	reWhitespace = regexp.MustCompile(`\s+`)
)

// StartHALVoice starts the HAL voice pipeline.
func (s *HermesService) StartHALVoice(deepgramKey, llmKey, sttKey, ttsKey, llmBaseURL, sttBaseURL, ttsBaseURL, ttsVoice, ttsInstructions, ttsProvider string) error {
	if deepgramKey == "" {
		return nil
	}
	if err := hal.StartVoice(hal.VoiceStartConfig{
		DeepgramKey:     deepgramKey,
		LLMKey:          llmKey,
		STTKey:          sttKey,
		TTSKey:          ttsKey,
		LLMBaseURL:      llmBaseURL,
		STTBaseURL:      sttBaseURL,
		TTSBaseURL:      ttsBaseURL,
		TTSVoice:        ttsVoice,
		TTSInstructions: ttsInstructions,
		TTSProvider:     ttsProvider,
	}); err != nil {
		return err
	}
	slog.Info("HAL voice pipeline started", "component", "hermes")
	flow.Log("voice_pipeline_start", nil)
	return nil
}

func stripForTTS(text string) string {
	text = reEmoji.ReplaceAllString(text, "")
	text = reMDBold.ReplaceAllString(text, "$1")
	text = reMDItalic.ReplaceAllString(text, "$1")
	text = reMDLink.ReplaceAllString(text, "$1")
	text = reCodeBlock.ReplaceAllString(text, "")
	text = reInlineCode.ReplaceAllString(text, "$1")
	text = reWhitespace.ReplaceAllString(text, " ")
	return strings.TrimSpace(text)
}

func truncRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n])
}

func (s *HermesService) SetVolume(pct int) error {
	if err := hal.SetVolume(pct); err != nil {
		return err
	}
	slog.Info("speaker volume set", "component", "hermes", "pct", pct)
	return nil
}

func (s *HermesService) StopTTS() error {
	if err := hal.StopTTS(); err != nil {
		return err
	}
	if err := hal.StopAudio(); err != nil {
		slog.Warn("stop audio failed", "component", "hermes", "error", err)
	}
	slog.Info("speaker stopped (TTS + music)", "component", "hermes")
	return nil
}

// Speak says text out loud and nothing else — no agent turn, no session entry, no tokens.
func (s *HermesService) Speak(text string) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	if err := hal.Speak(text); err != nil {
		return fmt.Errorf("speak: %w", err)
	}
	slog.Info("TTS spoken (verbatim)", "component", "hermes", "text", truncRunes(text, 80))
	s.monitorBus.Push(domain.MonitorEvent{Type: "tts", Summary: text})
	return nil
}

func (s *HermesService) SendToHALTTS(text string) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	if err := hal.SpeakReply(text); err != nil {
		return fmt.Errorf("speak: %w", err)
	}
	slog.Info("TTS sent", "component", "hermes", "text", truncRunes(text, 80))
	s.monitorBus.Push(domain.MonitorEvent{Type: "tts", Summary: text})
	return nil
}

func (s *HermesService) SendToHALTTSQueue(text string) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	if err := hal.SpeakQueueReply(text); err != nil {
		return fmt.Errorf("speak-queue: %w", err)
	}
	slog.Info("TTS queued", "component", "hermes", "text", truncRunes(text, 80))
	s.monitorBus.Push(domain.MonitorEvent{Type: "tts", Summary: text})
	return nil
}

func (s *HermesService) SendToHALTTSQueueForTurn(text, turnID string, turnSeq uint64) error {
	text = stripForTTS(text)
	if text == "" {
		return nil
	}
	if err := hal.SpeakQueueReplyForTurn(text, turnID, turnSeq); err != nil {
		return fmt.Errorf("speak-queue: %w", err)
	}
	slog.Info("TTS queued", "component", "hermes", "turn_id", turnID, "turn_seq", turnSeq, "text", truncRunes(text, 80))
	s.monitorBus.Push(domain.MonitorEvent{Type: "tts", Summary: text})
	return nil
}
