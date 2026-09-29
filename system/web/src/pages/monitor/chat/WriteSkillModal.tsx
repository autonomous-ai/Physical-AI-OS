import { useState } from "react";
import { PenLine, Check } from "lucide-react";
import { saveSkill } from "@/lib/api";
import { ModalShell } from "./ModalShell";
import { fieldLabel, inputStyle, btnStyle } from "./styles";

const NAME_PATTERN = /^[a-z0-9_-]+$/;
const NAME_MAX = 64;

export function WriteSkillModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedPath, setSavedPath] = useState("");

  const nameOk = NAME_PATTERN.test(name.trim()) && name.trim().length <= NAME_MAX;
  const canSave = nameOk && description.trim() !== "" && instructions.trim() !== "" && !saving;

  const submit = async () => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    try {
      const res = await saveSkill({
        name: name.trim(),
        description: description.trim(),
        instructions: instructions.trim(),
      });
      setSavedPath(res.path);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  return (
    <ModalShell
      icon={PenLine}
      title="Write skill"
      subtitle="Author a new skill for this agent"
      width={560}
      onClose={onClose}
      footer={savedPath ? (
        <button type="button" className="lm-u-btn lm-u-btn-primary" style={btnStyle} onClick={onClose}>Done</button>
      ) : (
        <>
          <button type="button" className="lm-u-btn" style={btnStyle} onClick={onClose}>Cancel</button>
          <button
            type="button"
            className="lm-u-btn lm-u-btn-primary"
            style={{ ...btnStyle, opacity: canSave ? 1 : 0.5, cursor: canSave ? "pointer" : "not-allowed" }}
            disabled={!canSave}
            onClick={submit}
          >{saving ? "Saving…" : "Save skill"}</button>
        </>
      )}
    >
      {savedPath ? (
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, padding: "36px 12px", textAlign: "center" }}>
          <Check size={28} style={{ color: "var(--lm-amber)" }} />
          <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--lm-text)" }}>
            /{name.trim()} saved
          </div>
          <div style={{
            fontSize: 10.5, color: "var(--lm-text-muted)",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", wordBreak: "break-all",
          }}>{savedPath}</div>
          <div style={{ fontSize: 11, color: "var(--lm-text-dim)", marginTop: 4, maxWidth: 380, lineHeight: 1.5 }}>
            The agent picks it up on its next session — no restart needed.
          </div>
        </div>
      ) : (
      <div style={{ display: "flex", flexDirection: "column", gap: 15 }}>
        <div>
          <label htmlFor="skill-name" style={fieldLabel}>Skill name</label>
          <input
            id="skill-name"
            autoFocus
            value={name}
            onChange={(e) => { setName(e.target.value); if (error) setError(""); }}
            placeholder="weekly-status-report"
            className="lm-u-input"
            style={inputStyle}
          />
          <div style={{
            fontSize: 10.5, marginTop: 5,
            color: name && !nameOk ? "var(--lm-red)" : "var(--lm-text-muted)",
          }}>
            {name && !nameOk
              ? "Lowercase letters, digits, _ and - only."
              : "Becomes the skill directory — the agent invokes it as /" + (nameOk ? name.trim() : "name") + "."}
          </div>
        </div>

        <div>
          <label htmlFor="skill-description" style={fieldLabel}>Description</label>
          <input
            id="skill-description"
            value={description}
            onChange={(e) => { setDescription(e.target.value); if (error) setError(""); }}
            placeholder="Summarise the week's activity into a short status report."
            className="lm-u-input"
            style={inputStyle}
          />
          <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 5 }}>
            One line. This is what the agent reads when deciding whether to load the skill.
          </div>
        </div>

        <div>
          <label htmlFor="skill-instructions" style={fieldLabel}>Instructions</label>
          <textarea
            id="skill-instructions"
            value={instructions}
            onChange={(e) => { setInstructions(e.target.value); if (error) setError(""); }}
            placeholder={"When the user asks for a weekly status report:\n1. Collect what happened since last Monday.\n2. Group it into Done / In progress / Blocked.\n3. Keep it under 10 bullets, no filler."}
            rows={10}
            className="lm-u-input"
            style={{ ...inputStyle, resize: "vertical", minHeight: 160, lineHeight: 1.55 }}
          />
          <div style={{ fontSize: 10.5, color: "var(--lm-text-muted)", marginTop: 5 }}>
            The body of SKILL.md — markdown, written as instructions to the agent.
          </div>
        </div>

        {error && (
          <div style={{
            fontSize: 11.5, color: "var(--lm-red)",
            background: "var(--lm-red-dim)",
            border: "1px solid var(--lm-red-glow)",
            borderRadius: 9, padding: "8px 10px",
          }}>{error}</div>
        )}
      </div>
      )}
    </ModalShell>
  );
}
