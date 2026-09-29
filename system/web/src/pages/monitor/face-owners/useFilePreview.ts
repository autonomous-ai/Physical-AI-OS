import { useRef, useState } from "react";
import { hwUrl } from "@/lib/api";
import { HW } from "../types";

// Folder-tree expand state, inline text preview, audio playback, and file open routing for the per-person file gallery.
export function useFilePreview() {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [preview, setPreview] = useState<{ label: string; path: string; content: string } | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const toggleDir = (key: string) => setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  const [playingAudio, setPlayingAudio] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const playAudio = (label: string, filepath: string) => {
    const key = `${label}/${filepath}`;
    if (playingAudio === key) {
      audioRef.current?.pause();
      setPlayingAudio(null);
      return;
    }
    if (audioRef.current) audioRef.current.pause();
    const audio = new Audio(`${HW}/face/file/${label}/${filepath}`);
    audio.onended = () => setPlayingAudio(null);
    audio.onerror = () => setPlayingAudio(null);
    audio.play().catch(() => setPlayingAudio(null));
    audioRef.current = audio;
    setPlayingAudio(key);
  };

  const downloadFile = (label: string, filepath: string) => {
    const a = document.createElement("a");
    a.href = `${HW}/face/file/${label}/${filepath}`;
    a.download = filepath.split("/").pop() || filepath;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const openFile = async (label: string, filepath: string) => {
    const isImg = /\.(jpg|jpeg|png|bmp)$/i.test(filepath);
    if (isImg) {
      window.open(
        hwUrl(`/face/photo/${encodeURIComponent(label)}/${encodeURIComponent(filepath)}`),
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    const isAudio = /\.(wav|mp3|ogg|webm)$/i.test(filepath);
    if (isAudio) {
      playAudio(label, filepath);
      return;
    }
    const isText = /\.(json|jsonl|txt|log|md|csv|yaml|yml|py|js|ts|tsx)$/i.test(filepath);
    if (!isText) {
      downloadFile(label, filepath);
      return;
    }
    if (preview?.label === label && preview?.path === filepath) {
      setPreview(null);
      return;
    }
    setPreviewLoading(true);
    try {
      const res = await fetch(`${HW}/face/file/${label}/${filepath}`);
      const text = await res.text();
      let content = text;
      if (/\.json$/i.test(filepath)) {
        try { content = JSON.stringify(JSON.parse(text), null, 2); } catch { /* leave raw */ }
      }
      setPreview({ label, path: filepath, content });
    } catch {
      setPreview({ label, path: filepath, content: "(failed to load)" });
    } finally {
      setPreviewLoading(false);
    }
  };

  return {
    expanded, toggleDir,
    preview, setPreview, previewLoading,
    playingAudio,
    openFile,
  };
}
