import { useEffect, useRef, useState } from "react";
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
    getTTSProviders().then((providers) => {
      setTtsProviders(providers);
      if (urlProvider && providers.length > 0 && !providers.includes(urlProvider)) {
        console.warn(`[setup] URL tts_provider="${urlProvider}" not in ${providers.join(",")}, using ${providers[0]}`);
        setTtsProvider(providers[0]);
      }
    }).catch(() => {});
    getTTSVoices().then(setTtsVoices).catch(() => {});
    // Intentional empty deps — mount-only, like the original effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const providerChangedByUser = useRef(false);
  const urlVoiceValidated = useRef(false);
  useEffect(() => {
    getTTSVoices(ttsProvider, sttLanguage).then((voices) => {
      setTtsVoices(voices);
      if (voices.length > 0 && !voices.includes(ttsVoice)) {
        const urlVoiceInvalid = !urlVoiceValidated.current && !!urlVoice;
        if (providerChangedByUser.current || urlVoiceInvalid) {
          if (urlVoiceInvalid) {
            console.warn(`[setup] URL tts_voice="${urlVoice}" not in voice list for provider=${ttsProvider} lang=${sttLanguage || "auto"}, using ${voices[0]}`);
          }
          setTtsVoice(voices[0]);
        }
      }
      urlVoiceValidated.current = true;
      providerChangedByUser.current = true;
    }).catch(() => {});
    // Refetch only on provider/language change; ttsVoice is written here and urlVoice is a one-shot prefill.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ttsProvider, sttLanguage]);

  return { ttsProviders, ttsVoices };
}
