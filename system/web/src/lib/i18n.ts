import { useSyncExternalStore } from "react";

export const LANG = {
  EN: "en",
  VI: "vi",
  ZH_CN: "zh-CN",
  ZH_TW: "zh-TW",
} as const;

export type Lang = (typeof LANG)[keyof typeof LANG];

const FALLBACK_LANG: Lang = LANG.EN;

// Normalises an STT language code or alias onto a canonical Lang (unknown -> English).
export function normalizeLang(code: string | undefined | null): Lang {
  if (!code) return FALLBACK_LANG;
  const c = code.toLowerCase();
  if (c === "vi" || c.startsWith("vi-")) return LANG.VI;
  if (c === "zh-tw" || c === "zh-hant" || c.startsWith("zh-hant-") || c === "zh-hk" || c === "zh-mo") return LANG.ZH_TW;
  if (c === "zh" || c === "zh-cn" || c === "zh-hans" || c.startsWith("zh-hans-") || c.startsWith("zh-")) return LANG.ZH_CN;
  if (c === "en" || c.startsWith("en-")) return LANG.EN;
  return FALLBACK_LANG;
}

let active: Lang = FALLBACK_LANG;
const listeners = new Set<() => void>();

// Sets the active language from a raw or alias code; no-op when unchanged.
export function setLanguage(code: string | undefined | null): void {
  const next = normalizeLang(code);
  if (next === active) return;
  active = next;
  for (const l of listeners) l();
}

// getLanguage returns the active canonical language.
export function getLanguage(): Lang {
  return active;
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

type Catalogue = Record<string, Partial<Record<Lang, string>> & { en: string }>;

const strings: Catalogue = {
  "chat.empty.title": {
    en: "Chat with Assistant",
    vi: "Trò chuyện với Assistant",
    "zh-CN": "与助手聊天",
    "zh-TW": "與助手聊天",
  },
  "chat.empty.subtitle": {
    en: "Ask anything, or try one of these",
    vi: "Hỏi bất cứ điều gì, hoặc thử một gợi ý",
    "zh-CN": "随便问，或试试以下建议",
    "zh-TW": "隨便問，或試試以下建議",
  },

  "chat.suggest.music": {
    en: "Play a relaxing song",
    vi: "Mở một bài nhạc thư giãn",
    "zh-CN": "播放一首轻松的歌",
    "zh-TW": "播放一首輕鬆的歌",
  },
  "chat.suggest.howAreYou": {
    en: "How are you today?",
    vi: "Hôm nay bạn thế nào?",
    "zh-CN": "你今天怎么样？",
    "zh-TW": "你今天好嗎？",
  },
  "chat.suggest.warmLight": {
    en: "Set a warm light mood",
    vi: "Chỉnh ánh sáng ấm áp",
    "zh-CN": "调成温暖的灯光",
    "zh-TW": "調成溫暖的燈光",
  },
  "chat.suggest.whatCanYouDo": {
    en: "What can you do?",
    vi: "Bạn làm được gì?",
    "zh-CN": "你能做什么？",
    "zh-TW": "你能做什麼？",
  },

  "chat.status.thinking": {
    en: "Assistant is thinking…",
    vi: "Assistant đang suy nghĩ…",
    "zh-CN": "助手正在思考…",
    "zh-TW": "助手正在思考…",
  },
  "chat.status.online": {
    en: "Assistant · online",
    vi: "Assistant · trực tuyến",
    "zh-CN": "助手 · 在线",
    "zh-TW": "助手 · 在線",
  },

  "chat.time.now": {
    en: "now",
    vi: "vừa xong",
    "zh-CN": "刚刚",
    "zh-TW": "剛剛",
  },
  "chat.time.minutes": {
    en: "{n}m",
    vi: "{n} phút",
    "zh-CN": "{n}分钟前",
    "zh-TW": "{n}分鐘前",
  },
  "chat.time.hours": {
    en: "{n}h",
    vi: "{n} giờ",
    "zh-CN": "{n}小时前",
    "zh-TW": "{n}小時前",
  },
  "chat.time.yesterday": {
    en: "yesterday",
    vi: "hôm qua",
    "zh-CN": "昨天",
    "zh-TW": "昨天",
  },
  "chat.time.days": {
    en: "{n}d",
    vi: "{n} ngày",
    "zh-CN": "{n}天前",
    "zh-TW": "{n}天前",
  },
};

// Looks up `key` in the active language, falling back to English, then to the key; `params` fills `{name}` placeholders.
export function t(key: string, params?: Record<string, string | number>, lang?: Lang): string {
  const entry = strings[key];
  if (!entry) return key;
  const l = lang ?? active;
  let out = entry[l] ?? entry.en;
  if (params) {
    for (const k in params) out = out.replace(`{${k}}`, String(params[k]));
  }
  return out;
}

// Returns a t() bound to the active language; re-renders when it changes.
export function useT(): (key: string, params?: Record<string, string | number>) => string {
  const lang = useSyncExternalStore(subscribe, getLanguage, getLanguage);
  return (key, params) => t(key, params, lang);
}
