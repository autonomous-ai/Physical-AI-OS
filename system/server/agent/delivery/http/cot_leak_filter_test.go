package http

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

const songPlanningLeak = `They named a song: "Eternal Flame" — likely the Bangles song. Play it.Speaker is unknown — no person to attribute. `
const songPlanningReply = `Playing Eternal Flame by The Bangles. Playing Eternal Flame — one of the all-time greats! Now that's what I call a favorite.`

func TestSongPlanningLeakEnglish(t *testing.T) {
	for _, lang := range []string{"", "en", "en-US"} {
		t.Run(lang, func(t *testing.T) {
			if got := newCoTLeakFilter(lang).filterText(songPlanningLeak + songPlanningReply); got != songPlanningReply {
				t.Fatalf("got %q, want %q", got, songPlanningReply)
			}
		})
	}
}

func TestSongPlanningFirstSentenceStream(t *testing.T) {
	h := &AgentHandler{
		config:           &config.Config{STTLanguage: "en"},
		assistantBuf:     make(map[string]*strings.Builder),
		streamedCleanLen: make(map[string]int),
	}
	for _, delta := range []string{`They named a song: "Eternal Flame" — likely the Bangles song. `, `Play it.Speaker is unknown — no person to attribute. `} {
		h.accumulateAssistantDelta("song", delta)
		if got := h.tryFirstSentenceFlush("song"); got != "" {
			t.Fatalf("planning reached streaming TTS: %q", got)
		}
		if _, marked := h.streamedCleanLen["song"]; marked {
			t.Fatal("planning must not mark the run as spoken")
		}
	}
	h.accumulateAssistantDelta("song", "Playing Eternal Flame by The Bangles. Enjoy")
	if got := h.tryFirstSentenceFlush("song"); got != "Playing Eternal Flame by The Bangles." {
		t.Fatalf("clean confirmation did not stream: %q", got)
	}
}

func TestSongPlanningDoesNotDropOrdinaryDiscussion(t *testing.T) {
	for _, text := range []string{
		`They named the song after their hometown.`,
		`The Bluetooth speaker is unknown to this app.`,
		`You asked for Eternal Flame. Playing it now.`,
	} {
		if got := newCoTLeakFilter("en").filterText(text); got != text {
			t.Errorf("ordinary reply changed: %q -> %q", text, got)
		}
	}
}

// Real DeepSeek leak (openclaw + deepseek): English planning monologue ahead of the reply.
const deepseekLeak = "The `[emotion_context]` shows `mapped_mood: \"sad\"`, `suggestion_worthy: true`, " +
	"`is_decision_stale: false`, `audio_playing: false`, `last_suggestion_age_min: 19` (not in cooldown). " +
	"Route = **music**. I need to log the signal, synthesize a decision (prior decision is `happy` 19min ago, " +
	"not stale; weak sad cue doesn't outweigh recent mixed signals — keep `happy`), and fire a music suggestion. " +
	"No `telegram_id` in user_info, so speak only. " +
	"[nhẹ nhàng] Có vẻ hơi trầm — muốn nghe một bài nhạc hoa nhẹ nhàng không Leo? 🎵"

func TestDeepseekLeakVietnamese(t *testing.T) {
	f := newCoTLeakFilter("vi")
	got := f.filterText(deepseekLeak)
	if !strings.Contains(got, "Có vẻ hơi trầm") {
		t.Fatalf("real reply was dropped: %q", got)
	}
	for _, leak := range []string{"emotion_context", "Route", "I need to", "telegram_id", "user_info"} {
		if strings.Contains(got, leak) {
			t.Errorf("leak fragment %q survived: %q", leak, got)
		}
	}
	if len(f.dropped) == 0 {
		t.Error("expected dropped sentences to be recorded")
	}
}

func TestDeepseekLeakEnglishModeStillCatchesIdentifiers(t *testing.T) {
	// Unset language = English mode: marker tiers plus the snake_case trigger.
	f := newCoTLeakFilter("")
	got := f.filterText(deepseekLeak)
	for _, leak := range []string{"emotion_context", "telegram_id"} {
		if strings.Contains(got, leak) {
			t.Errorf("snake_case sentence survived in English mode: %q", got)
		}
	}
	if !strings.Contains(got, "Có vẻ hơi trầm") {
		t.Fatalf("real reply was dropped: %q", got)
	}
}

func TestGeminiStyleTriggerAndContinuation(t *testing.T) {
	text := "The user is insisting on a song. Phrasing draft: \"Dạ anh Leo ơi, em mở nhạc nhé.\" " +
		"Keep it to 2 sentences. Dạ anh Leo ơi, em mở nhạc nhé!"
	f := newCoTLeakFilter("vi")
	got := f.filterText(text)
	if got != "Dạ anh Leo ơi, em mở nhạc nhé!" {
		t.Fatalf("got %q", got)
	}
}

func TestLegitVietnameseReplyUntouched(t *testing.T) {
	text := "Chào Leo! Hôm nay bạn thế nào? Mình có thể mở một bài nhạc nhẹ nếu bạn muốn."
	f := newCoTLeakFilter("vi")
	if got := f.filterText(text); !strings.Contains(got, "Chào Leo!") ||
		!strings.Contains(got, "mở một bài nhạc nhẹ") {
		t.Fatalf("legit reply mangled: %q", got)
	}
}

func TestLegitEnglishReplyUntouched(t *testing.T) {
	text := "Sure! I'll play some soft music for you. Let me know if you want something else."
	f := newCoTLeakFilter("en")
	got := f.filterText(text)
	for _, want := range []string{"soft music", "something else"} {
		if !strings.Contains(got, want) {
			t.Fatalf("legit English reply mangled: %q", got)
		}
	}
}

func TestEnglishLookingDroppedOnlyInCoTMode(t *testing.T) {
	// Before a trigger an English sentence on a Vietnamese device is kept; after one it is dropped.
	f := newCoTLeakFilter("vi")
	pre := f.filterText("Let it be is a great song by The Beatles.")
	if pre == "" {
		t.Fatal("English sentence dropped without CoT mode")
	}
	f2 := newCoTLeakFilter("vi")
	got := f2.filterText("The user wants music. This should be short and warm. Mình mở nhạc nhé!")
	if got != "Mình mở nhạc nhé!" {
		t.Fatalf("got %q", got)
	}
}

func TestFuzzyDraftDedup(t *testing.T) {
	f := newCoTLeakFilter("vi")
	got := f.filterText("Mình sẽ mở bài nhạc hoa nhẹ nhàng cho bạn nhé! " +
		"The user wants music now. Mình sẽ mở bài nhạc hoa nhẹ nhàng cho bạn ngay nhé!")
	if strings.Count(got, "nhạc hoa") != 1 {
		t.Fatalf("near-duplicate draft not deduped: %q", got)
	}
}

func TestSeededPrefixContinuity(t *testing.T) {
	// CoT mode must carry over the streamed-prefix boundary.
	prefix := "The user is asking about the weather."
	remainder := "I should keep this brief. Trời hôm nay nắng đẹp lắm!"
	f := newCoTLeakFilter("vi")
	f.filterText(prefix)
	got := strings.TrimSpace(f.filterText(remainder))
	if got != "Trời hôm nay nắng đẹp lắm!" {
		t.Fatalf("got %q", got)
	}
}

func TestGluedEnderSplit(t *testing.T) {
	// A missing space after an ender must not drag the real answer into the label.
	f := newCoTLeakFilter("vi")
	got := f.filterText("The user wants a nudge. Finalize response.Khả năng là bạn hơi mệt rồi đó.")
	if got != "Khả năng là bạn hơi mệt rồi đó." {
		t.Fatalf("got %q", got)
	}
}

func TestDecimalsAndLowercaseGlueIntact(t *testing.T) {
	f := newCoTLeakFilter("vi")
	text := "Phiên bản Node.js là 3.5 nhé bạn."
	if got := f.filterText(text); got != text {
		t.Fatalf("got %q", got)
	}
}

func TestLanguageCodeMapping(t *testing.T) {
	for code, want := range map[string]struct{ nonEnglish, nonASCII bool }{
		"":      {false, false},
		"en":    {false, false},
		"vi":    {true, true},
		"zh-CN": {true, true},
		"ja":    {true, true},
		"fr":    {true, false},
		"id":    {true, false},
	} {
		f := newCoTLeakFilter(code)
		if f.nonEnglish != want.nonEnglish || f.nonASCIIScript != want.nonASCII {
			t.Errorf("code %q: nonEnglish=%v nonASCIIScript=%v, want %+v",
				code, f.nonEnglish, f.nonASCIIScript, want)
		}
	}
}

func TestAudioTagOnlySentenceSurvives(t *testing.T) {
	// Pure audio tags steer TTS and must survive CoT mode.
	f := newCoTLeakFilter("vi")
	got := f.filterText("The user wants comfort. [nhẹ nhàng] Không sao đâu, mình ở đây với bạn mà.")
	if !strings.Contains(got, "[nhẹ nhàng]") || !strings.Contains(got, "Không sao đâu") {
		t.Fatalf("got %q", got)
	}
}
