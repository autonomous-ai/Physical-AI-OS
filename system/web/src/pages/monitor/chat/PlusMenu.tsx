import { useCallback, useEffect, useRef, useState } from "react";
import {
  Plus, Paperclip, Sparkles, ChevronRight, PenLine, Store, FolderTree, Upload,
  CalendarClock, CalendarPlus, ListChecks,
} from "lucide-react";
import { MenuDivider, MenuItem } from "./MenuPanel";
import { menuPanel } from "./styles";

export type SkillsAction = "write" | "upload" | "browse" | "manage";

export type ScheduledAction = "new" | "manage";

export function PlusMenu({
  disabled, onAttachFile, onSkillsAction, onCreateWithAgent, onScheduledAction,
}: {
  disabled: boolean;
  onAttachFile: () => void;
  onSkillsAction: (action: SkillsAction) => void;
  onCreateWithAgent: () => void;
  onScheduledAction: (action: ScheduledAction) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [scheduledOpen, setScheduledOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // Derived closed while disabled rather than closed from an effect.
  const open = menuOpen && !disabled;

  const close = useCallback(() => {
    setMenuOpen(false);
    setSkillsOpen(false);
    setScheduledOpen(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, close]);

  const run = (fn: () => void) => { close(); fn(); };

  return (
    <div ref={wrapRef} style={{ position: "relative", flexShrink: 0 }}>
      <button
        onClick={() => (open ? close() : setMenuOpen(true))}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        style={{
          background: open ? "color-mix(in srgb, var(--lm-text) 8%, transparent)" : "transparent",
          border: "none",
          borderRadius: "50%", width: 34, height: 34,
          cursor: disabled ? "default" : "pointer",
          color: open ? "var(--lm-text)" : "var(--lm-text-muted)",
          opacity: disabled ? 0.5 : 0.85, transition: "opacity 0.15s, background 0.15s, transform 0.15s",
          display: "inline-flex", alignItems: "center", justifyContent: "center",
          transform: open ? "rotate(45deg)" : "none",
        }}
        onMouseEnter={(e) => { if (!disabled) { e.currentTarget.style.opacity = "1"; e.currentTarget.style.background = "color-mix(in srgb, var(--lm-text) 8%, transparent)"; } }}
        onMouseLeave={(e) => { e.currentTarget.style.opacity = disabled ? "0.5" : "0.85"; if (!open) e.currentTarget.style.background = "transparent"; }}
        title="More"
        aria-label="More options"
      ><Plus size={18} /></button>

      {open && (
        <div role="menu" className="lm-pop" style={{ ...menuPanel, bottom: 42, left: 0, minWidth: 190 }}>
          <MenuItem icon={Paperclip} label="Attach file" hint="max 10 MB" onClick={() => run(onAttachFile)} />

          <div
            onMouseEnter={() => { setSkillsOpen(true); setScheduledOpen(false); }}
            style={{ position: "relative" }}
          >
            <MenuItem
              icon={Sparkles}
              label="Skills"
              active={skillsOpen}
              trailing={<ChevronRight size={14} style={{ opacity: 0.7 }} />}
              onClick={() => setSkillsOpen((v) => !v)}
            />
            {skillsOpen && (
              <div role="menu" className="lm-pop" style={{ ...menuPanel, bottom: -6, left: "calc(100% + 6px)", minWidth: 208 }}>
                <MenuItem icon={Sparkles} label="Create with Agent" hint="plan it together in chat" onClick={() => run(onCreateWithAgent)} />
                <MenuItem icon={PenLine} label="Write skill" hint="author a new SKILL.md" onClick={() => run(() => onSkillsAction("write"))} />
                <MenuItem icon={Upload} label="Upload a skill" hint=".skill / .zip / .md from this computer" onClick={() => run(() => onSkillsAction("upload"))} />
                <MenuDivider />
                <MenuItem icon={Store} label="Browse skills" hint="Autonomous skill store" onClick={() => run(() => onSkillsAction("browse"))} />
                <MenuItem icon={FolderTree} label="Manage skills" hint="installed on this runtime" onClick={() => run(() => onSkillsAction("manage"))} />
              </div>
            )}
          </div>

          <div
            onMouseEnter={() => { setScheduledOpen(true); setSkillsOpen(false); }}
            style={{ position: "relative" }}
          >
            <MenuItem
              icon={CalendarClock}
              label="Scheduled"
              active={scheduledOpen}
              trailing={<ChevronRight size={14} style={{ opacity: 0.7 }} />}
              onClick={() => setScheduledOpen((v) => !v)}
            />
            {scheduledOpen && (
              <div role="menu" className="lm-pop" style={{ ...menuPanel, bottom: -6, left: "calc(100% + 6px)", minWidth: 208 }}>
                <MenuItem icon={CalendarPlus} label="New scheduled task" hint="run something on a schedule" onClick={() => run(() => onScheduledAction("new"))} />
                <MenuItem icon={ListChecks} label="Manage scheduled tasks" hint="edit, pause or remove" onClick={() => run(() => onScheduledAction("manage"))} />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
