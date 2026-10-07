package i18n

import (
	"strings"
	"sync"
)

// chitchatInputs holds per-language exact-match inputs for chitchat intents; replies live in phrases.
var chitchatInputs = map[Phrase]map[string][]string{
	PhraseChitchatGreeting: {
		LangJA:   {"こんにちは", "おはよう", "おはようございます", "こんばんは", "やあ", "こんにちは{name}"},
		LangVI:   {"chào", "chào {name}", "{name} ơi"},
		LangEN:   {"hi", "hello", "hey", "hi {name}", "hello {name}", "hey {name}"},
		LangZhCN: {"你好", "你好啊", "嗨", "嘿"},
		LangZhTW: {"你好", "嗨"},
	},
	PhraseChitchatFarewell: {
		LangJA:   {"さようなら", "またね", "バイバイ"},
		LangVI:   {"tạm biệt", "tạm biệt {name}"},
		LangEN:   {"bye", "bye {name}", "goodbye", "see you", "see ya", "later"},
		LangZhCN: {"再见", "拜拜"},
		LangZhTW: {"再見", "拜拜"},
	},
	PhraseChitchatThanks: {
		LangJA:   {"ありがとう", "ありがとうございます", "ありがとう{name}"},
		LangVI:   {"cảm ơn", "cảm ơn {name}"},
		LangEN:   {"thanks", "thank you", "thanks {name}", "thx"},
		LangZhCN: {"谢谢", "谢谢你"},
		LangZhTW: {"謝謝", "謝謝你"},
	},
	PhraseChitchatApology: {
		LangJA:   {"ごめん", "ごめんなさい", "すみません"},
		LangVI:   {"xin lỗi", "tớ xin lỗi", "mình xin lỗi", "lỗi của mình"},
		LangEN:   {"sorry", "i'm sorry", "im sorry", "my bad", "apologies"},
		LangZhCN: {"对不起", "抱歉"},
		LangZhTW: {"對不起", "抱歉"},
	},
	PhraseChitchatCompliment: {
		LangJA:   {"すごい", "かわいい", "よくできたね", "えらいね"},
		LangVI:   {"giỏi quá", "giỏi ghê", "xinh quá", "xinh ghê", "dễ thương quá", "đáng yêu quá", "tuyệt vời"},
		LangEN:   {"good job", "good {name}", "good girl", "good boy", "well done", "nice job", "great job", "you're cute", "you're awesome"},
		LangZhCN: {"真棒", "棒棒哒", "好可爱"},
		LangZhTW: {"真棒", "好可愛"},
	},
	PhraseChitchatNevermind: {
		LangJA:   {"なんでもない", "気にしないで", "やっぱりいい"},
		LangVI:   {"thôi", "thôi quên đi", "thôi bỏ đi", "bỏ đi", "không sao"},
		LangEN:   {"never mind", "nevermind", "forget it", "drop it", "no matter"},
		LangZhCN: {"算了"},
		LangZhTW: {"算了"},
	},
	PhraseChitchatPresenceCheck: {
		LangJA:   {"いる？", "いる", "そこにいる", "聞こえる", "まだいる"},
		LangVI:   {"còn đó không", "còn đó hông", "vẫn còn đó chứ", "có nghe không"},
		LangEN:   {"are you there", "are you still there", "you still there", "you there"},
		LangZhCN: {"在吗", "你在吗", "还在吗"},
		LangZhTW: {"在嗎", "你在嗎", "還在嗎"},
	},
}

// InputPhrases returns per-language inputs for chitchat phrase p, or nil.
func InputPhrases(p Phrase) map[string][]string {
	in := chitchatInputs[p]
	if in == nil {
		return nil
	}
	out := make(map[string][]string, len(in))
	for lang, list := range in {
		out[lang] = applyNameAll(list)
	}
	return out
}

// ChitchatPhrases returns the chitchat phrase keys in match order.
func ChitchatPhrases() []Phrase {
	return []Phrase{
		// Specific phrases first so generic greeting/farewell do not shadow them.
		PhraseChitchatPresenceCheck,
		PhraseChitchatApology,
		PhraseChitchatCompliment,
		PhraseChitchatGreeting,
		PhraseChitchatFarewell,
		PhraseChitchatThanks,
		PhraseChitchatNevermind,
	}
}

// chitchatCommandWords are per-language action words that disqualify a chitchat match.
var chitchatCommandWords = map[string][]string{
	LangJA: {"つけて", "消して", "再生", "止めて", "変えて", "開いて", "閉じて", "教えて", "読んで", "歌って", "探して", "見せて", "撮って", "音楽", "ライト"},
	LangVI: {
		"bật", "tắt", "mở", "đóng", "phát", "dừng", "đổi", "chuyển",
		"chụp", "kể", "đọc", "hát", "hỏi", "tìm", "xem", "nói",
		"to lên", "nhỏ lại", "lớn hơn", "nhỏ hơn", "im lặng",
		"nhạc", "đèn", "ảnh",
	},
	LangEN: {
		"turn", "play", "stop", "switch", "change", "open", "close", "set",
		"show", "take", "tell", "read", "sing", "find", "search", "ask",
		"louder", "softer", "mute", "unmute", "lights", "music", "song",
	},
	LangZhCN: {"开", "关", "播放", "停", "换", "唱", "讲", "找", "拍", "看"},
	LangZhTW: {"開", "關", "播放", "停", "換", "唱", "講", "找", "拍", "看"},
}

// ChitchatCommandWords returns command words across all languages, flattened.
func ChitchatCommandWords() []string {
	var out []string
	for _, ws := range chitchatCommandWords {
		out = append(out, ws...)
	}
	return out
}

// chitchatWakeWords are name tokens stripped from the head of input before chitchat matching.
var (
	chitchatWakeMu    sync.RWMutex
	chitchatWakeWords []string
)

// BuildChitchatWakeWords derives local-intent attention tokens from name, longest first.
func BuildChitchatWakeWords(name string) []string {
	n := strings.ToLower(strings.TrimSpace(name))
	if n == "" {
		return nil
	}
	return []string{
		"hello " + n, "hey " + n, "này " + n, "ê " + n, n + " ơi",
		n,
	}
}

// BuildVoiceWakeWords derives the English-prefix aliases for HAL's STT wake-word gate.
func BuildVoiceWakeWords(name string) []string {
	n := strings.ToLower(strings.TrimSpace(name))
	if n == "" {
		return nil
	}
	return []string{
		"wake up " + n, "hello " + n, "okay " + n, "hey " + n,
		"hi " + n, "alo " + n, "ok " + n,
	}
}

// BuildSupportedVoiceWakeWords returns every phrase HAL's wake-word gate accepts.
// Keep aligned with hal/drivers/voice/_internal/config.py and merge_wake_words.
func BuildSupportedVoiceWakeWords(agentName, deviceType string) []string {
	words := make([]string, 0, 21)
	seen := make(map[string]struct{}, 21)
	for _, name := range []string{"autonomous", deviceType, agentName} {
		for _, word := range BuildVoiceWakeWords(name) {
			if _, ok := seen[word]; ok {
				continue
			}
			seen[word] = struct{}{}
			words = append(words, word)
		}
	}
	return words
}

// SetChitchatWakeWords replaces the wake-word strip list.
func SetChitchatWakeWords(words []string) {
	chitchatWakeMu.Lock()
	chitchatWakeWords = words
	chitchatWakeMu.Unlock()
}

// ChitchatWakeWords returns the current wake-word list, longest first.
func ChitchatWakeWords() []string {
	chitchatWakeMu.RLock()
	defer chitchatWakeMu.RUnlock()
	return chitchatWakeWords
}
