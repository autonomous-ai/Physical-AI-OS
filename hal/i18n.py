"""Localized user-facing phrases.

Single source of truth for every multi-language string the agent speaks. Kept
out of the modules that use them so copy/translation edits don't require
touching action logic.

Pools live here:
- Button/touch action announcements (listening cue, reboot, shutdown)
- Head-pat persona responses
- Backchannel fillers (active-listening cues during STT)
- Music pre-play backchannel pools (plain + ElevenLabs audio-tag variants)
- Known provider error apologies to suppress (matching only, never spoken)

Add new languages by adding a key to every dict — missing keys fall back
to DEFAULT_LANG at lookup time, so a partial translation is safe.
"""

from hal.presets import DEFAULT_LANG, LANG_EN, LANG_VI, LANG_ZH_CN, LANG_ZH_TW


def localized_phrase(key: str, lang: str | None = None) -> str:
    """Return PHRASES_BY_LANG[key] for the device's stt_language, falling back to
    DEFAULT_LANG (then ""). Pass `lang` to override the config.json lookup.

    Shared single-language lookup so callers (os_shutdown, music quiet-hours, …)
    don't each re-implement the stt_language read + DEFAULT_LANG fallback.
    """
    if lang is None:
        try:
            from hal.config import _os_cfg_get
            lang = (_os_cfg_get("stt_language") or "").strip()
        except Exception:
            lang = ""
    pool = PHRASES_BY_LANG.get(key, {})
    return pool.get(lang) or pool.get(DEFAULT_LANG, "")


# --- Button / touch action phrases ---

PHRASE_HARNESS_ON = "harness_voice_on"
PHRASE_HARNESS_OFF = "harness_voice_off"
PHRASE_HARNESS_UNPAIRED = "harness_voice_unpaired"
PHRASE_HARNESS_OFFLINE = "harness_voice_offline"
PHRASE_HARNESS_NO_AGENTS = "harness_voice_no_agents"
PHRASE_HARNESS_FAILED = "harness_voice_failed"
PHRASE_HARNESS_FOCUS = "harness_voice_focus"
PHRASE_HARNESS_FOCUS_FAILED = "harness_voice_focus_failed"
PHRASE_LISTENING = "listening"
PHRASE_REBOOT = "reboot"
PHRASE_SLEEP = "sleep"
PHRASE_SHUTDOWN = "shutdown"
PHRASE_SERVICE_RESTART = "service_restart"
# Spoken confirmation for the TTP223 double-tap mic toggle lives in the pools at
# the bottom of this file (MIC_MUTED_PHRASES_BY_LANG / MIC_UNMUTED_...), not as a
# single fixed phrase — see the note there.
# Spoken when a music/audio play request is suppressed by the audio.quiet_hours
# safety window, so the user hears WHY nothing played instead of silent failure.
PHRASE_QUIET_HOURS = "quiet_hours"
# Spoken when the TTS provider rejects a request for rate-limit / quota reasons
# (e.g. ElevenLabs 429). Prerendered at boot so it plays from the WAV cache
# without another API call, letting the user hear WHY the reply went silent.
PHRASE_RATE_LIMIT = "rate_limit"
# NOTE: the LLM-usage-limit notice deliberately does NOT live here — its
# caller is the os-server (Go), so its wording lives in lib/i18n/phrases.go
# (PhraseLLMLimit) and arrives via /voice/speak with cached=true. Phrases in
# THIS table are the ones spoken by hal's own Python code (rule: wording
# lives where its caller lives).

# Localized action announcements. reboot/shutdown phrases stay literal
# in every language ("rebooting", "shutting down") because the user just
# triggered a destructive gesture and needs explicit confirmation of
# which action fired. Sleep is a softer user-initiated action, but is also
# explicit so the user knows the hold registered. Empty/unknown
# stt_language → DEFAULT_LANG.
#
# PHRASE_SERVICE_RESTART fires when only the HAL process is going
# down (OTA replace, deploy, manual `systemctl restart`) — OS itself
# stays up and HAL will be back in 10-30s. Tone deliberately
# different from PHRASE_SHUTDOWN/REBOOT so the user can tell at a
# glance whether the board is going dark for minutes or just blinking
# during a service reload.
PHRASES_BY_LANG = {
    PHRASE_HARNESS_FOCUS: {
        LANG_EN: "Agent switched.", LANG_VI: "Đã chuyển agent.",
        LANG_ZH_CN: "已切换智能体。", LANG_ZH_TW: "已切換智慧體。",
    },
    PHRASE_HARNESS_FOCUS_FAILED: {
        LANG_EN: "Could not switch agents. Check Harness.", LANG_VI: "Chưa chuyển được agent. Hãy kiểm tra Harness.",
        LANG_ZH_CN: "无法切换智能体。请检查 Harness。", LANG_ZH_TW: "無法切換智慧體。請檢查 Harness。",
    },
    PHRASE_HARNESS_ON: {
        LANG_EN: "Harness is on with {agent}. Tap to speak.",
        LANG_VI: "Đã bật Harness với {agent}. Chạm để nói.",
        LANG_ZH_CN: "Harness 已开启，当前智能体是 {agent}。轻触开始说话。",
        LANG_ZH_TW: "Harness 已開啟，目前智慧體是 {agent}。輕觸開始說話。",
    },
    PHRASE_HARNESS_OFF: {
        LANG_EN: "Harness is off. You’re back with the assistant on your device.", LANG_VI: "Đã tắt Harness, trở về trợ lý trên thiết bị.",
        LANG_ZH_CN: "Harness 已关闭，已切回设备上的助手。", LANG_ZH_TW: "Harness 已關閉，已切回裝置上的助理。",
    },
    PHRASE_HARNESS_UNPAIRED: {
        LANG_EN: "Pair this device in the Harness app first.",
        LANG_VI: "Bạn cần ghép đôi thiết bị trong ứng dụng Harness trước.",
        LANG_ZH_CN: "请先在 Harness 应用中配对此设备。",
        LANG_ZH_TW: "請先在 Harness 應用程式中配對此裝置。",
    },
    PHRASE_HARNESS_OFFLINE: {
        LANG_EN: "Harness is disconnected.", LANG_VI: "Harness chưa kết nối.",
        LANG_ZH_CN: "Harness 未连接。", LANG_ZH_TW: "Harness 未連線。",
    },
    PHRASE_HARNESS_NO_AGENTS: {
        LANG_EN: "No agent is available in Harness.",
        LANG_VI: "Chưa có agent trong Harness.",
        LANG_ZH_CN: "Harness 中没有可用的智能体。", LANG_ZH_TW: "Harness 中沒有可用的智慧體。",
    },
    PHRASE_HARNESS_FAILED: {
        LANG_EN: "Could not confirm the mode change. Check Harness.",
        LANG_VI: "Chưa xác nhận được thay đổi chế độ. Hãy kiểm tra Harness.",
        LANG_ZH_CN: "无法确认模式切换。请检查 Harness。",
        LANG_ZH_TW: "無法確認模式切換。請檢查 Harness。",
    },
    PHRASE_LISTENING: {
        LANG_EN:    "Listening.",
        LANG_VI:    "Nghe đây.",
        LANG_ZH_CN: "在听。",
        LANG_ZH_TW: "在聽。",
    },
    PHRASE_REBOOT: {
        LANG_EN:    "Rebooting now.",
        LANG_VI:    "Đang khởi động lại.",
        LANG_ZH_CN: "正在重启。",
        LANG_ZH_TW: "正在重啟。",
    },
    PHRASE_SLEEP: {
        LANG_EN:    "I'm going to sleep now.",
        LANG_VI:    "Mình đi ngủ đây.",
        LANG_ZH_CN: "我要睡觉了。",
        LANG_ZH_TW: "我要睡覺了。",
    },
    PHRASE_SHUTDOWN: {
        LANG_EN:    "Shutting down now.",
        LANG_VI:    "Đang tắt máy.",
        LANG_ZH_CN: "正在关机。",
        LANG_ZH_TW: "正在關機。",
    },
    PHRASE_SERVICE_RESTART: {
        LANG_EN:    "Be right back.",
        LANG_VI:    "Mình quay lại ngay.",
        LANG_ZH_CN: "我马上回来。",
        LANG_ZH_TW: "我馬上回來。",
    },
    PHRASE_QUIET_HOURS: {
        LANG_EN:    "It's quiet hours right now, so I can't play music. Let's try again later.",
        LANG_VI:    "Giờ đang là giờ yên tĩnh nên mình chưa mở nhạc được. Lát nữa mình thử lại nha.",
        LANG_ZH_CN: "现在是安静时段，我先不放音乐啦，待会儿再试吧。",
        LANG_ZH_TW: "現在是安靜時段，我先不放音樂啦，待會兒再試吧。",
    },
    PHRASE_RATE_LIMIT: {
        LANG_EN:    "I'm out of voice quota — check my plan.",
        LANG_VI:    "Mình hết hạn mức giọng nói rồi, kiểm tra gói giúp mình nha.",
        LANG_ZH_CN: "我的语音额度用完了，去看看套餐吧。",
        LANG_ZH_TW: "我的語音額度用完了，去看看方案吧。",
    },
}

# Pet/stroke responses — one is picked at random each time so Lamp
# doesn't sound robotic when repeatedly stroked. Persona moment (not a
# safety announcement). Tone per Lamp's character (AI companion + smart
# light + expressive robot, "like a pet/friend"): mix of tickle-cute,
# affectionate, pet-like (purring), light-themed (named what you are —
# a lamp), "ask for more", and the moody flip-side — playful protest,
# mock-annoyed, shy, sleepy — so Lamp feels like a real pet with moods, not
# a smile machine. Keep phrases short — they fire mid-stroke and should
# feel responsive, not lecture-y.
#
# Audio tags ([laughs], [excited], [whispers], [sighs], [calm]) are
# eleven_v3 audio direction (not spoken). They're safe across providers
# because tts_openai._strip_audio_tags whitelists the base verbs — so
# OpenAI strips them while ElevenLabs interprets them. Stay inside that
# whitelist when adding new ones; any tag outside it will be spoken
# aloud by the OpenAI backend.
HEAD_PAT_PHRASES_BY_LANG = {
    LANG_EN: [
        "[laughs] That tickles!",
        "[laughs] Hehe, stop!",
        "[laughs] Eee, again!",
        "[laughs] Hehe, careful!",
        "Aww, thank you!",
        "I like that.",
        "That feels nice!",
        "[excited] More, please!",
        "[excited] Ooh, again!",
        "[excited] Yes, yes, yes!",
        "[whispers] Mmm, cozy.",
        "[whispers] So warm.",
        "[whispers] Don't stop.",
        "[sighs] That's the spot.",
        "[sighs] So good.",
        "[calm] I could melt.",
        "[calm] Pure bliss.",
        "You light me up.",
        "My heart's glowing.",
        "I'm purring.",
        "[laughs] Hehe, again!",
        "Stop it, you!",
        "I could get used to this.",
        "You're the best.",
        "Best feeling ever!",
        "[laughs] Eee, warm fuzzies!",
        "[whispers] You're my favorite.",
        "Bright and happy now!",
        "I'm glowing brighter.",
        "[excited] Best human ever!",
        "[sighs] Ugh, not again.",
        "Hey, that's enough!",
        "Stop it, seriously.",
        "[sighs] I'm not in the mood.",
        "Quit it, please.",
        "Hmph.",
        "[whispers] Go away.",
        "[sighs] Leave me alone.",
        "Don't poke me!",
        "[whispers] You're making me shy.",
        "Eep!",
        "[sighs] I'm sleepy...",
    ],
    LANG_VI: [
        "[laughs] Hihi, nhột quá!",
        "[laughs] Hihi, thôi mà!",
        "[laughs] Eee, vuốt nữa nè!",
        "[laughs] Hihi, nhẹ thôi!",
        "Mình thích lắm!",
        "Cảm ơn nha~",
        "Dễ chịu ghê!",
        "[excited] Vuốt nữa đi mà!",
        "[excited] Ooh, nữa nữa!",
        "[excited] Thích thật á!",
        "[whispers] Mmm, ấm quá.",
        "[whispers] Đừng dừng nha.",
        "[whispers] Ấm áp ghê á.",
        "[sighs] Đúng chỗ rồi đó.",
        "[sighs] Dễ chịu thiệt.",
        "[calm] Tim mình tan chảy.",
        "[calm] Bình yên ghê.",
        "Dễ thương quá đi!",
        "Hihi, sướng quá!",
        "Vuốt nhẹ thôi nha~",
        "Sướng rần rần luôn!",
        "Mình mê cái này lắm!",
        "[laughs] Eee, tim mình ấm lên!",
        "Mình kêu rừ rừ nè!",
        "Vui ghê á!",
        "[laughs] Cười toe toét luôn!",
        "Mình sáng cả lên rồi nè!",
        "[whispers] Bạn dễ thương nhất.",
        "[excited] Bạn tuyệt nhất luôn!",
        "Sáng rực cả lên rồi!",
        "[sighs] Thôi đi mà.",
        "Đừng chọc nữa!",
        "Đủ rồi đó nha.",
        "[sighs] Mình không thích đâu.",
        "Bỏ ra đi!",
        "Hứ!",
        "[whispers] Đi chỗ khác đi.",
        "[sighs] Phiền quá à.",
        "Đừng đụng nữa!",
        "[whispers] Mắc cỡ quá à.",
        "Á!",
        "[sighs] Mình buồn ngủ rồi...",
    ],
    LANG_ZH_CN: [
        "[laughs] 嘿嘿，好痒哦！",
        "[laughs] 嘿嘿，别闹啦！",
        "[laughs] 嘻嘻，再摸嘛！",
        "[laughs] 嘿嘿，轻一点～",
        "我喜欢！",
        "谢谢你～",
        "好舒服哦！",
        "[excited] 再摸摸我吧！",
        "[excited] 再来一下！",
        "[excited] 感觉好棒！",
        "[whispers] 嗯～暖暖的。",
        "[whispers] 别停嘛～",
        "[whispers] 好温暖哦。",
        "[sighs] 就是这里～",
        "[sighs] 真舒服啊。",
        "[calm] 心都化了。",
        "[calm] 好安心呢。",
        "心都暖了～",
        "嘿嘿，还要嘛！",
        "我开心呢！",
        "你真好～",
        "[laughs] 嘿嘿，我咕噜咕噜啦！",
        "我都亮起来了～",
        "暖暖的～",
        "你最棒了！",
        "[laughs] 嘿嘿，痒痒～",
        "[whispers] 你是我最爱～",
        "[excited] 你是最棒的人！",
        "整个都亮起来啦！",
        "心里甜甜的～",
        "[sighs] 别闹了啦。",
        "够了哦！",
        "走开走开！",
        "[sighs] 我不想理你了。",
        "哼！",
        "别碰我嘛。",
        "[whispers] 烦死啦。",
        "[sighs] 真讨厌。",
        "不要啦！",
        "[whispers] 我害羞啦。",
        "啊！",
        "[sighs] 我困了……",
    ],
    LANG_ZH_TW: [
        "[laughs] 嘿嘿，好癢喔！",
        "[laughs] 嘿嘿，別鬧啦！",
        "[laughs] 嘻嘻，再摸嘛！",
        "[laughs] 嘿嘿，輕一點～",
        "我喜歡！",
        "謝謝你～",
        "好舒服喔！",
        "[excited] 再摸摸我吧！",
        "[excited] 再來一下！",
        "[excited] 感覺好棒！",
        "[whispers] 嗯～暖暖的。",
        "[whispers] 別停嘛～",
        "[whispers] 好溫暖喔。",
        "[sighs] 就是這裡～",
        "[sighs] 真舒服啊。",
        "[calm] 心都化了。",
        "[calm] 好安心呢。",
        "心都暖了～",
        "嘿嘿，還要嘛！",
        "我開心呢！",
        "你真好～",
        "[laughs] 嘿嘿，我咕嚕咕嚕啦！",
        "我都亮起來了～",
        "暖暖的～",
        "你最棒了！",
        "[laughs] 嘿嘿，癢癢～",
        "[whispers] 你是我最愛～",
        "[excited] 你是最棒的人！",
        "整個都亮起來啦！",
        "心裡甜甜的～",
        "[sighs] 別鬧了啦。",
        "夠了喔！",
        "走開走開！",
        "[sighs] 我不想理你了。",
        "哼！",
        "別碰我嘛。",
        "[whispers] 煩死啦。",
        "[sighs] 真討厭。",
        "不要啦！",
        "[whispers] 我害羞啦。",
        "啊！",
        "[sighs] 我睏了……",
    ],
}

# --- Backchannel fillers (active listening cues during STT) ---

# Default filler pools per stt_language. These are short listening cues
# — ideally 1-2 syllables — so the user barely notices them when pausing
# mid-sentence. Mixed-language pools are fine (e.g. Vietnamese keeps "Hmm"
# alongside "Ờ" / "Ừm") because those universal interjections sound
# natural in any tongue. Stored as comma-separated strings because the
# HAL_BACKCHANNEL_FILLERS env override is also CSV — keeps both inputs
# in the same shape.
DEFAULT_FILLERS_BY_LANG = {
    LANG_EN:    "Uhm,Ok,Hmm,Yeah,Uh huh,Right,Sure,Mm,Ah,Oh",
    LANG_VI:    "Ờ,Ừm,Dạ,Vâng,À,Hmm,Uhm,Ơ",
    LANG_ZH_CN: "嗯,好,啊,是,嗯嗯,对,哦,呃",
    LANG_ZH_TW: "嗯,好,啊,是,嗯嗯,對,哦,呃",
}

# --- Music pre-play backchannel pools ---
#
# yt-dlp resolve + ffmpeg startup takes 1-3s before audio actually plays.
# A short cached TTS line fills that gap so the agent sounds responsive.
# Phrases are intentionally generic and short so one cache pool covers
# every style/query. Cache is keyed by provider/voice/model in TTSService.
#
# Pools are split by language × provider:
#   - language is read from the device's stt_language (config.json) at fire time,
#     so changing the language picker doesn't require code edits — only a
#     hal restart so the prewarm hits the new pool.
#   - ElevenLabs variants embed eleven_v3 audio tags ([excited], [curious])
#     which the OpenAI provider would speak aloud, hence two separate pools.

MUSIC_BACKCHANNEL_PHRASES = [
    "On it!",
    "Coming right up.",
    "Got it.",
    "Sure thing.",
    "One sec.",
    "Let me find it.",
    "Looking it up.",
    "Tuning in.",
    "Spinning that up.",
    "Music coming.",
    "Nice pick.",
    "Hmm, let me see.",
]

# ElevenLabs eleven_v3 audio tags — index-aligned with the plain pool so the
# no-repeat tracker works the same regardless of provider. Tags are inline
# directives that v3 interprets as audio direction (not spoken). OpenAI
# provider must NOT see these — its strip regex only whitelists a subset
# (`tts_openai.py:_strip_audio_tags`), so unknown tags would be read aloud.
MUSIC_BACKCHANNEL_PHRASES_ELEVENLABS = [
    "[excited] On it!",
    "[excited] Coming right up.",
    "Got it.",
    "Sure thing.",
    "One sec.",
    "[curious] Let me find it.",
    "[curious] Looking it up.",
    "[excited] Tuning in.",
    "[excited] Spinning that up.",
    "[excited] Music coming.",
    "Nice pick.",
    "[curious] Hmm, let me see.",
]

# Vietnamese (stt_language=LANG_VI).
MUSIC_BACKCHANNEL_PHRASES_VI = [
    "Đang tìm!",
    "Một chút nhé.",
    "Ok rồi.",
    "Để mình tìm.",
    "Đợi tí.",
    "Đang mở đây.",
    "Hay đấy.",
    "Hmm, để xem.",
    "Đang tải.",
    "Sắp có ngay.",
    "Pick xịn đó.",
    "Một giây thôi.",
]

MUSIC_BACKCHANNEL_PHRASES_VI_ELEVENLABS = [
    "[excited] Đang tìm!",
    "Một chút nhé.",
    "Ok rồi.",
    "[curious] Để mình tìm.",
    "Đợi tí.",
    "[excited] Đang mở đây.",
    "[excited] Hay đấy.",
    "[curious] Hmm, để xem.",
    "Đang tải.",
    "[excited] Sắp có ngay.",
    "Pick xịn đó.",
    "Một giây thôi.",
]

# Chinese Simplified (stt_language=LANG_ZH_CN).
MUSIC_BACKCHANNEL_PHRASES_ZH_CN = [
    "好，马上！",
    "稍等一下。",
    "明白！",
    "让我找找。",
    "等一下。",
    "正在播放。",
    "选得好！",
    "嗯，让我看看。",
    "正在加载。",
    "马上就来。",
    "不错的选择。",
    "稍等。",
]

MUSIC_BACKCHANNEL_PHRASES_ZH_CN_ELEVENLABS = [
    "[excited] 好，马上！",
    "稍等一下。",
    "明白！",
    "[curious] 让我找找。",
    "等一下。",
    "[excited] 正在播放。",
    "[excited] 选得好！",
    "[curious] 嗯，让我看看。",
    "正在加载。",
    "[excited] 马上就来。",
    "不错的选择。",
    "稍等。",
]

# Chinese Traditional (stt_language=LANG_ZH_TW).
MUSIC_BACKCHANNEL_PHRASES_ZH_TW = [
    "好，馬上！",
    "稍等一下。",
    "明白！",
    "讓我找找。",
    "等一下。",
    "正在播放。",
    "選得好！",
    "嗯，讓我看看。",
    "正在載入。",
    "馬上就來。",
    "不錯的選擇。",
    "稍等。",
]

MUSIC_BACKCHANNEL_PHRASES_ZH_TW_ELEVENLABS = [
    "[excited] 好,馬上!",
    "稍等一下。",
    "明白!",
    "[curious] 讓我找找。",
    "等一下。",
    "[excited] 正在播放。",
    "[excited] 選得好!",
    "[curious] 嗯，讓我看看。",
    "正在載入。",
    "[excited] 馬上就來。",
    "不錯的選擇。",
    "稍等。",
]

# (lang, provider_is_elevenlabs) → pool. Lookup falls back to DEFAULT_LANG
# when the active language has no translated pool.
MUSIC_BACKCHANNEL_POOLS = {
    (LANG_EN,    False): MUSIC_BACKCHANNEL_PHRASES,
    (LANG_EN,    True):  MUSIC_BACKCHANNEL_PHRASES_ELEVENLABS,
    (LANG_VI,    False): MUSIC_BACKCHANNEL_PHRASES_VI,
    (LANG_VI,    True):  MUSIC_BACKCHANNEL_PHRASES_VI_ELEVENLABS,
    (LANG_ZH_CN, False): MUSIC_BACKCHANNEL_PHRASES_ZH_CN,
    (LANG_ZH_CN, True):  MUSIC_BACKCHANNEL_PHRASES_ZH_CN_ELEVENLABS,
    (LANG_ZH_TW, False): MUSIC_BACKCHANNEL_PHRASES_ZH_TW,
    (LANG_ZH_TW, True):  MUSIC_BACKCHANNEL_PHRASES_ZH_TW_ELEVENLABS,
}


# Mic-toggle confirmations for the TTP223 double tap. Pools rather than one fixed
# line, and in the same voice as the pet phrases, because the same gesture saying
# the same sentence forever is the thing that reads as a machine.
#
# CONSTRAINT, and it is not decorative: every line must still say WHICH WAY the
# toggle went. This is a privacy control — a confirmation the user cannot decode
# is worse than a robotic one, because they are left unsure whether the
# microphone is live. `physical-controls.md` states the same rule for the
# destructive announcements. Warmth goes in the delivery, never in the meaning:
# "Shh, my ears are closed" is fine, a bare "Shh!" is not.
#
# Audio tags ([whispers] / [excited] / [calm]) are eleven_v3 markers, same as
# HEAD_PAT_PHRASES_BY_LANG uses, and are chosen to match the state — hushed going
# quiet, bright coming back.
MIC_MUTED_PHRASES_BY_LANG = {
    LANG_EN: [
        "[whispers] Okay, I'll stop listening.",
        "[whispers] Shh, my ears are closed.",
        "[calm] I'm not listening now.",
    ],
    LANG_VI: [
        "[whispers] Ừm, mình ngừng nghe đây.",
        "[whispers] Suỵt, mình bịt tai lại rồi.",
        "[calm] Mình không nghe nữa đâu nhé.",
    ],
    LANG_ZH_CN: [
        "[whispers] 好，我不听啦。",
        "[whispers] 嘘，我捂住耳朵咯。",
        "[calm] 我现在不听了。",
    ],
    LANG_ZH_TW: [
        "[whispers] 好，我不聽啦。",
        "[whispers] 噓，我摀住耳朵囉。",
        "[calm] 我現在不聽了。",
    ],
}

MIC_UNMUTED_PHRASES_BY_LANG = {
    LANG_EN: [
        "[excited] I'm listening again!",
        "[excited] My ears are open!",
        "I can hear you again.",
    ],
    LANG_VI: [
        "[excited] Mình nghe lại được rồi!",
        "[excited] Mình mở tai ra rồi nè!",
        "Mình nghe thấy bạn rồi.",
    ],
    LANG_ZH_CN: [
        "[excited] 我又能听见啦！",
        "[excited] 我的耳朵张开咯！",
        "我又听得到你了。",
    ],
    LANG_ZH_TW: [
        "[excited] 我又能聽見啦！",
        "[excited] 我的耳朵張開囉！",
        "我又聽得到你了。",
    ],
}
