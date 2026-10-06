import { useEffect, useState } from "react";
import { getTTSProviders, getTTSVoices } from "@/lib/api";

// Manages the TTS provider+voice dropdowns for Setup.
export function useTTSCatalog({
  ttsProvider,
  sttLanguage,
  ttsVoice,
  urlProvider,
  urlVoice,
  setTtsProvider,
  setTtsVoice,
}: {
  ttsProvider: string;
  sttLanguage: string;
  ttsVoice: string;
  urlProvider: string;
  urlVoice: string;
  setTtsProvider: (v: string) => void;
  setTtsVoice: (v: string) => void;
}) {
  const [ttsProviders, setTtsProviders] = useState<string[]>([]);
  const [ttsVoices, setTtsVoices] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    getTTSProviders().then((providers) => {
      if (cancelled) return;
      setTtsProviders(providers);
      if (urlProvider && providers.length > 0 && !providers.includes(urlProvider)) {
        console.warn(`[setup] URL tts_provider="${urlProvider}" not in ${providers.join(",")}, using ${providers[0]}`);
        setTtsProvider(providers[0]);
      }
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [urlProvider, setTtsProvider]);

  useEffect(() => {
    let cancelled = false;
    getTTSVoices(ttsProvider, sttLanguage).then((voices) => {
      if (cancelled) return;
      setTtsVoices(voices);
      // Validate the initial default too, including a Japanese browser's first visit.
      if (voices.length > 0 && !voices.includes(ttsVoice)) {
        if (urlVoice && ttsVoice === urlVoice) {
          console.warn(`[setup] URL tts_voice="${urlVoice}" not in voice list for provider=${ttsProvider} lang=${sttLanguage || "auto"}, using ${voices[0]}`);
        }
        setTtsVoice(voices[0]);
      }
    }).catch(() => {});
    // A previous provider/language response must not overwrite the current selection.
    return () => { cancelled = true; };
  }, [ttsProvider, sttLanguage, ttsVoice, urlVoice, setTtsVoice]);

  return { ttsProviders, ttsVoices };
}
