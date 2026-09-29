import { useState } from "react";
import { FileText, Download } from "lucide-react";
import { agentFileUrl } from "@/lib/api";

// Device paths the agent named, rendered as images or download chips.
const ROOTS = String.raw`(?:/root/\.[a-z0-9_-]+/(?:media|workspace)|/tmp)`;

const IMAGE_EXT = ["jpg", "jpeg", "png", "gif", "webp"];
const OTHER_EXT = ["pdf", "txt", "md", "csv", "wav", "mp3", "mp4", "webm"];

const FILE_RE = new RegExp(
  `${ROOTS}/[^\\s"'\`)<>\\]]+\\.(${[...IMAGE_EXT, ...OTHER_EXT].join("|")})\\b`,
  "gi",
);

/** The parts of a tool chip that can name a file. */
interface FileBearingTool {
  args?: Record<string, unknown>;
  detail?: string;
  result?: string;
}

// Device paths named anywhere in the turn (reply text and tool args), de-duplicated, in order.
function extractAgentFiles(text: string, tools?: FileBearingTool[]): string[] {
  const haystacks: string[] = [text || ""];
  for (const t of tools ?? []) {
    if (t.args) {
      try {
        haystacks.push(JSON.stringify(t.args));
      } catch { /* circular/unserializable args — nothing to scan */ }
    }
    if (t.detail) haystacks.push(t.detail);
    if (t.result) haystacks.push(t.result);
  }

  const seen = new Set<string>();
  for (const hay of haystacks) {
    for (const m of hay.matchAll(FILE_RE)) {
      seen.add(m[0].replace(/[.,;:]+$/, ""));
    }
  }
  return [...seen];
}

export function AgentFiles({ text, tools }: { text: string; tools?: FileBearingTool[] }) {
  const paths = extractAgentFiles(text, tools);
  if (paths.length === 0) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      {paths.map((p) => <AgentFile key={p} path={p} />)}
    </div>
  );
}

function AgentFile({ path }: { path: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;

  const name = path.slice(path.lastIndexOf("/") + 1);
  const ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  const url = agentFileUrl(path);

  if (IMAGE_EXT.includes(ext)) {
    return (
      <a href={url} target="_blank" rel="noreferrer noopener" title={path}>
        <img
          src={url}
          alt={name}
          onError={() => setFailed(true)}
          style={{
            maxWidth: "100%", maxHeight: 320, borderRadius: 10, display: "block",
            border: "1px solid var(--lm-border)",
          }}
        />
      </a>
    );
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      download={name}
      title={path}
      style={{
        display: "inline-flex", alignItems: "center", gap: 9, alignSelf: "flex-start",
        maxWidth: "100%", padding: "8px 11px", borderRadius: 10,
        background: "var(--lm-card)", border: "1px solid var(--lm-border)",
        color: "var(--lm-text)", textDecoration: "none",
      }}
    >
      <FileText size={15} style={{ color: "var(--lm-amber)", flexShrink: 0, alignSelf: "flex-start", marginTop: 1 }} />
      <span style={{
        fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
      }}>{name}</span>
      <Download size={13} style={{ color: "var(--lm-text-dim)", flexShrink: 0 }} />
    </a>
  );
}
