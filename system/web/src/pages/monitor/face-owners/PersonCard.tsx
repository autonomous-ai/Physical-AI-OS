import type { CSSProperties, Dispatch, SetStateAction } from "react";
import { Pencil, Trash2, History, ChevronDown, ChevronRight, X } from "lucide-react";
import { hwUrl } from "@/lib/api";
import type { FaceOwnerDetail } from "../types";

// One enrolled-person card.
export function PersonCard({
  person, idx, currentUser,
  expandedPerson, setExpandedPerson,
  hoveredPerson, setHoveredPerson,
  hoveredPhoto, setHoveredPhoto,
  expanded, toggleDir,
  deleting, deletingPhoto,
  preview, previewLoading, setPreview,
  playingAudio,
  onRename, onRemove, onRemovePhoto, onRemoveVoiceFile, onOpenFile, onTimeline,
  monCard, iconBtnStyle,
}: {
  person: FaceOwnerDetail;
  idx: number;
  currentUser: string;
  expandedPerson: Record<string, boolean>;
  setExpandedPerson: Dispatch<SetStateAction<Record<string, boolean>>>;
  hoveredPerson: string | null;
  setHoveredPerson: Dispatch<SetStateAction<string | null>>;
  hoveredPhoto: string | null;
  setHoveredPhoto: Dispatch<SetStateAction<string | null>>;
  expanded: Record<string, boolean>;
  toggleDir: (key: string) => void;
  deleting: string | null;
  deletingPhoto: string | null;
  preview: { label: string; path: string; content: string } | null;
  previewLoading: boolean;
  setPreview: (p: { label: string; path: string; content: string } | null) => void;
  playingAudio: string | null;
  onRename: (label: string) => void;
  onRemove: (label: string) => void;
  onRemovePhoto: (label: string, filename: string) => void;
  onRemoveVoiceFile: (label: string, filename: string) => void;
  onOpenFile: (label: string, filepath: string) => void;
  onTimeline: (label: string) => void;
  monCard: CSSProperties;
  iconBtnStyle: CSSProperties;
}) {
  const isCurrent = !!currentUser && currentUser === person.label;
  const isExpanded = expandedPerson[person.label] ?? isCurrent;
  const cardStyle: React.CSSProperties = isCurrent
    ? {
        ...monCard,
        border: "2px solid var(--lm-teal)",
        boxShadow: "0 0 12px var(--lm-teal-glow)",
      }
    : monCard;
  return (
    <div
      className="lm-mon-card lm-card-in"
      style={{ ...cardStyle, ["--lm-stagger" as string]: `${Math.min(idx, 8) * 45}ms` }}
      onMouseEnter={() => setHoveredPerson(person.label)}
      onMouseLeave={() => setHoveredPerson((cur) => (cur === person.label ? null : cur))}
    >

      <div
        onClick={() => setExpandedPerson((p) => ({ ...p, [person.label]: !isExpanded }))}
        style={{
          display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          cursor: "pointer",
          margin: "-16px -16px 12px -16px",
          padding: "11px 14px",
          background: "color-mix(in srgb, var(--lm-text) 5%, transparent)",
          borderBottom: "1px solid var(--lm-border)",
          borderTopLeftRadius: 12,
          borderTopRightRadius: 12,
        }}
      >
        {(() => {
          const avatarBorder = isCurrent ? "var(--lm-teal)" : "var(--lm-border)";
          const firstPhoto = person.photos?.[0];
          return (
            <div style={{
              width: 42, height: 42, borderRadius: "50%", flexShrink: 0,
              display: "inline-flex", alignItems: "center", justifyContent: "center",
              background: "var(--lm-amber-dim)", color: "var(--lm-amber)",
              border: `2px solid ${avatarBorder}`,
              fontSize: 16, fontWeight: 700, textTransform: "uppercase",
              overflow: "hidden",
              boxShadow: isCurrent ? "0 0 8px var(--lm-teal-glow)" : "none",
            }}>
              {firstPhoto ? (
                <img
                  src={hwUrl(`/face/photo/${encodeURIComponent(person.label)}/${encodeURIComponent(firstPhoto)}`)}
                  alt=""
                  style={{ width: "100%", height: "100%", objectFit: "cover" }}
                  onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                />
              ) : (person.label[0] ?? "?")}
            </div>
          );
        })()}
        <div style={{
          fontSize: 13, fontWeight: 700,
          color: "var(--lm-amber)",
          textTransform: "capitalize",
        }}>
          {person.label}
        </div>
        {isCurrent && (
          <span className="lm-pulse" style={{
            fontSize: 10, padding: "2px 6px", borderRadius: 4,
            background: "var(--lm-teal)", color: "var(--lm-on-amber)",
            fontWeight: 700, letterSpacing: 0.5,
          }}>● HERE NOW</span>
        )}
        <span style={{ flex: 1 }} />
        {(() => {
          const isHovered = hoveredPerson === person.label;
          const hoverStyle: React.CSSProperties = {
            opacity: isHovered ? 1 : 0,
            pointerEvents: isHovered ? "auto" : "none",
            transition: "opacity 0.15s ease",
          };
          return (
            <>
              {person.label !== "unknown" && (
                <button
                  onClick={(e) => { e.stopPropagation(); onRename(person.label); }}
                  title="Rename"
                  aria-label="Rename"
                  className="lm-u-btn"
                  style={{ ...iconBtnStyle, ...hoverStyle }}
                ><Pencil size={14} /></button>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); onTimeline(person.label); }}
                title="Timeline"
                aria-label="Timeline"
                style={{
                  ...iconBtnStyle,
                  background: "color-mix(in srgb, var(--lm-blue) 15%, transparent)",
                  color: "var(--lm-blue)",
                  border: "1px solid color-mix(in srgb, var(--lm-blue) 30%, transparent)",
                  ...hoverStyle,
                }}
              ><History size={14} /></button>
              <button
                onClick={(e) => { e.stopPropagation(); onRemove(person.label); }}
                disabled={deleting === person.label}
                title="Delete user"
                aria-label="Delete user"
                style={{
                  ...iconBtnStyle,
                  background: "color-mix(in srgb, var(--lm-red) 12%, transparent)",
                  color: "var(--lm-red)",
                  border: "1px solid color-mix(in srgb, var(--lm-red) 35%, transparent)",
                  cursor: deleting === person.label ? "not-allowed" : "pointer",
                  opacity: deleting === person.label ? 0.5 : (isHovered ? 1 : 0),
                  pointerEvents: isHovered ? "auto" : "none",
                  transition: "opacity 0.15s ease",
                }}
              >{deleting === person.label ? "…" : <Trash2 size={14} />}</button>
              <span style={{
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                width: 18, height: 18, color: "var(--lm-text-muted)",
              }}>
                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              </span>
            </>
          );
        })()}
      </div>

      {(person.telegram_username || person.telegram_id) && (
        <div style={{ fontSize: 10, color: "var(--lm-text-muted)", marginBottom: 12 }}>
          {person.telegram_username && <span>@{person.telegram_username}</span>}
          {person.telegram_username && person.telegram_id && <span> · </span>}
          {person.telegram_id && <span>ID: {person.telegram_id}</span>}
        </div>
      )}

      {(() => {
        const audioCount = person.voice_samples?.filter((f) => /\.(wav|mp3|ogg)$/i.test(f)).length ?? 0;
          const tags: Array<{ n: number | string; label: string; full: string; color: string }> = [
            { n: person.photo_count, label: "photos", full: `${person.photo_count} face photos`, color: "var(--lm-amber)" },
          ];
          if (person.mood_days?.length)              tags.push({ n: person.mood_days.length,             label: "mood",     full: `${person.mood_days.length} mood days`,             color: "var(--lm-green)"  });
          if (person.wellbeing_days?.length)         tags.push({ n: person.wellbeing_days.length,        label: "wb",       full: `${person.wellbeing_days.length} wellbeing days`,    color: "var(--lm-blue)"   });
          if (person.music_suggestion_days?.length)  tags.push({ n: person.music_suggestion_days.length, label: "music",    full: `${person.music_suggestion_days.length} music suggestion days`, color: "var(--lm-purple)" });
          if (person.posture_days?.length)           tags.push({ n: person.posture_days.length,          label: "posture",  full: `${person.posture_days.length} posture days`,        color: "var(--lm-cyan, #06b6d4)" });
          if (person.audio_history_days?.length)     tags.push({ n: person.audio_history_days.length,    label: "audio",    full: `${person.audio_history_days.length} audio history days`, color: "var(--lm-blue)" });
          if (person.habit_patterns)                 tags.push({ n: "✓",                                  label: "habit",    full: "Habit patterns recorded",                            color: "var(--lm-amber)"  });
          if (audioCount > 0)                        tags.push({ n: audioCount,                          label: "voice",    full: `${audioCount} voice samples`,                       color: "var(--lm-purple)" });

          return (
            <div style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
              {tags.map((t, i) => (
                <span
                  key={i}
                  title={t.full}
                  style={{
                    display: "inline-flex", alignItems: "baseline", gap: 4,
                    padding: "3px 8px", borderRadius: 999,
                    background: `color-mix(in srgb, ${t.color} 10%, transparent)`,
                    border: `1px solid color-mix(in srgb, ${t.color} 22%, transparent)`,
                  }}
                >
                  <span style={{ fontSize: 11, fontWeight: 700, color: t.color, lineHeight: 1 }}>{t.n}</span>
                  <span style={{ fontSize: 9.5, fontWeight: 500, color: t.color, opacity: 0.72, textTransform: "uppercase", letterSpacing: "0.04em", lineHeight: 1 }}>{t.label}</span>
                </span>
              ))}
            </div>
          );
        })()}

      {isExpanded && (<>
      <div style={{
        fontFamily: "monospace",
        fontSize: 11,
        lineHeight: 1.7,
        color: "var(--lm-text-muted)",
      }}>
        {person.photos.length > 0 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
            {person.photos.map((photo) => {
              const delKey = `${person.label}/${photo}`;
              const isDeleting = deletingPhoto === delKey;
              const isHovered = hoveredPhoto === delKey;
              return (
                <div
                  key={photo}
                  title={photo}
                  onMouseEnter={() => setHoveredPhoto(delKey)}
                  onMouseLeave={() => setHoveredPhoto((cur) => (cur === delKey ? null : cur))}
                  style={{ position: "relative", width: 56, height: 56 }}
                >
                  <img
                    src={hwUrl(`/face/photo/${encodeURIComponent(person.label)}/${encodeURIComponent(photo)}`)}
                    style={{
                      width: "100%", height: "100%",
                      objectFit: "cover",
                      borderRadius: 6,
                      border: "1px solid var(--lm-border)",
                      display: "block",
                      cursor: "pointer",
                    }}
                    onClick={() => onOpenFile(person.label, photo)}
                    onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                  />
                  <button
                    onClick={(e) => { e.stopPropagation(); onRemovePhoto(person.label, photo); }}
                    disabled={isDeleting}
                    title={`Remove ${photo}`}
                    style={{
                      position: "absolute", top: 3, right: 3,
                      width: 20, height: 20,
                      borderRadius: 5,
                      background: "rgba(0,0,0,0.55)",
                      color: "#fff",
                      border: "none",
                      cursor: isDeleting ? "wait" : "pointer",
                      padding: 0,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      opacity: isDeleting ? 0.5 : (isHovered ? 1 : 0),
                      pointerEvents: isHovered ? "auto" : "none",
                      transition: "opacity 0.15s ease",
                      backdropFilter: "blur(2px)",
                    }}
                  >
                    <X size={12} strokeWidth={2.5} />
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {(() => {
          const items: { name: string; isDir?: boolean; dirKey?: string; children?: string[]; filePath?: string }[] = [];
          person.files?.filter((f) => !person.photos.includes(f)).forEach((f) => items.push({ name: f, filePath: f }));
          if (person.mood_days && person.mood_days.length > 0) {
            items.push({ name: "mood", isDir: true, dirKey: `${person.label}:mood`, children: person.mood_days.map((d) => `${d}.jsonl`) });
          }
          if (person.wellbeing_days && person.wellbeing_days.length > 0) {
            items.push({ name: "wellbeing", isDir: true, dirKey: `${person.label}:wellbeing`, children: person.wellbeing_days.map((d) => `${d}.jsonl`) });
          }
          if (person.music_suggestion_days && person.music_suggestion_days.length > 0) {
            items.push({ name: "music-suggestions", isDir: true, dirKey: `${person.label}:music-suggestions`, children: person.music_suggestion_days.map((d) => `${d}.jsonl`) });
          }
          if (person.posture_days && person.posture_days.length > 0) {
            items.push({ name: "posture", isDir: true, dirKey: `${person.label}:posture`, children: person.posture_days.map((d) => `${d}.jsonl`) });
          }
          if (person.audio_history_days && person.audio_history_days.length > 0) {
            items.push({ name: "audio_history", isDir: true, dirKey: `${person.label}:audio_history`, children: person.audio_history_days.map((d) => `${d}.jsonl`) });
          }
          if (person.habit_patterns) {
            items.push({ name: "habit", isDir: true, dirKey: `${person.label}:habit`, children: ["patterns.json"] });
          }
          if (person.voice_samples && person.voice_samples.length > 0) {
            items.push({ name: "voice", isDir: true, dirKey: `${person.label}:voice`, children: person.voice_samples });
          }
          return items.map((item, i) => {
            const isLastTop = i === items.length - 1;
            const prefix = isLastTop ? "└── " : "├── ";
            if (item.isDir && item.dirKey) {
              const isOpen = expanded[item.dirKey] ?? false;
              return (
                <div key={item.name}>
                  <span
                    style={{ cursor: "pointer" }}
                    onClick={() => toggleDir(item.dirKey!)}
                  >
                    <span style={{ color: "var(--lm-text-dim)" }}>{prefix}</span>
                    <span style={{ color: "var(--lm-green)" }}>{isOpen ? "▾" : "▸"}</span>
                    <span style={{ color: "var(--lm-green)", fontWeight: 600 }}> {item.name}/</span>
                  </span>
                  {isOpen && item.children?.map((child, ci) => {
                    const childPrefix = isLastTop ? "    " : "│   ";
                    const childBranch = ci === (item.children?.length ?? 0) - 1 ? "└── " : "├── ";
                    const childPath = `${item.name}/${child}`;
                    const isActive = preview?.label === person.label && preview?.path === childPath;
                    const isChildAudio = /\.(wav|mp3|ogg|webm)$/i.test(child);
                    const audioKey = `${person.label}/${childPath}`;
                    const isPlaying = playingAudio === audioKey;
                    // Only voice audio is deletable; metadata.json/.npy would corrupt the profile.
                    const canDelete = item.name === "voice" && isChildAudio && person.label !== "unknown";
                    const deleteKey = `${person.label}/voice/${child}`;
                    const isDeleting = deletingPhoto === deleteKey;
                    return (
                      <div key={child} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                        <span
                          style={{ cursor: "pointer" }}
                          onClick={() => onOpenFile(person.label, childPath)}
                        >
                          <span style={{ color: "var(--lm-text-dim)" }}>{childPrefix}{childBranch}</span>
                          {isChildAudio && (
                            <span style={{ color: isPlaying ? "var(--lm-amber)" : "var(--lm-purple)", marginRight: 4 }}>
                              {isPlaying ? "⏸" : "▶"}
                            </span>
                          )}
                          <span style={{
                            color: isActive || isPlaying ? "var(--lm-amber)" : "inherit",
                            textDecoration: "underline",
                            textDecorationStyle: "dotted" as const,
                            textUnderlineOffset: 3,
                          }}>{child}</span>
                        </span>
                        {canDelete && (
                          <span
                            onClick={(e) => { e.stopPropagation(); onRemoveVoiceFile(person.label, child); }}
                            title={`Remove ${child}`}
                            style={{
                              cursor: isDeleting ? "wait" : "pointer",
                              fontSize: 10,
                              color: "var(--lm-red)",
                              opacity: isDeleting ? 0.5 : 0.6,
                              fontWeight: 600,
                            }}
                          >✕</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              );
            }
            const isActive = preview?.label === person.label && preview?.path === item.filePath;
            return (
              <div key={item.name} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                <span
                  style={{ cursor: "pointer" }}
                  onClick={() => onOpenFile(person.label, item.filePath!)}
                >
                  <span style={{ color: "var(--lm-text-dim)" }}>{prefix}</span>
                  <span style={{
                    color: isActive ? "var(--lm-amber)" : "inherit",
                    textDecoration: "underline",
                    textDecorationStyle: "dotted" as const,
                    textUnderlineOffset: 3,
                  }}>{item.name}</span>
                </span>
              </div>
            );
          });
        })()}
      </div>

      {preview && preview.label === person.label && (
        <div style={{
          marginTop: 8,
          padding: "8px 10px",
          borderRadius: 6,
          background: "var(--lm-surface)",
          border: "1px solid var(--lm-border)",
          fontSize: 10,
          fontFamily: "monospace",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
          maxHeight: 200,
          overflowY: "auto",
          color: "var(--lm-text)",
          position: "relative",
        }}>
          <div style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: 6,
            paddingBottom: 4,
            borderBottom: "1px solid var(--lm-border)",
          }}>
            <span style={{ color: "var(--lm-amber)", fontWeight: 600 }}>{preview.path}</span>
            <span
              style={{ cursor: "pointer", color: "var(--lm-text-muted)", fontSize: 12 }}
              onClick={() => setPreview(null)}
            >x</span>
          </div>
          {previewLoading ? "Loading..." : preview.content}
        </div>
      )}
      </> )}{/* /isExpanded */}
    </div>
  );
}
