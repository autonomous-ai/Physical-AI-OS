import { useEffect, useState } from "react";
import { toast } from "sonner";
import { C, SectionCard, LABEL_STYLE, INPUT_STYLE } from "@/components/setup/shared";
import { listMCPTools, addMCPTool, removeMCPTool } from "@/lib/api";
import type { MCPTool } from "@/lib/api";

type HeaderRow = { key: string; value: string };

export function MCPToolsSection({ active }: { active: boolean }) {
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [headers, setHeaders] = useState<HeaderRow[]>([]);

  useEffect(() => {
    listMCPTools()
      .then(setTools)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  function addHeaderRow() { setHeaders((prev) => [...prev, { key: "", value: "" }]); }
  function removeHeaderRow(i: number) { setHeaders((prev) => prev.filter((_, idx) => idx !== i)); }
  function updateHeader(i: number, field: "key" | "value", val: string) {
    setHeaders((prev) => prev.map((h, idx) => idx === i ? { ...h, [field]: val } : h));
  }

  function buildHeaders(): Record<string, string> | undefined {
    const h: Record<string, string> = {};
    for (const r of headers) {
      const k = r.key.trim();
      const v = r.value.trim();
      if (k && v) h[k] = v;
    }
    return Object.keys(h).length > 0 ? h : undefined;
  }

  async function handleAdd() {
    const n = name.trim();
    const u = url.trim();
    if (!n || !u) { toast.error("Name and URL are required."); return; }
    setAdding(true);
    const hdrs = buildHeaders();
    try {
      await addMCPTool({ name: n, url: u, headers: hdrs });
      setTools((prev) => [...prev, { name: n, url: u, headers: hdrs }]);
      setName("");
      setUrl("");
      setHeaders([]);
      toast.success(`Added MCP tool "${n}".`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to add tool.");
    } finally {
      setAdding(false);
    }
  }

  async function handleRemove(toolName: string) {
    setRemoving(toolName);
    try {
      await removeMCPTool(toolName);
      setTools((prev) => prev.filter((t) => t.name !== toolName));
      toast.success(`Removed "${toolName}".`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to remove tool.");
    } finally {
      setRemoving(null);
    }
  }

  const headerCount = (t: MCPTool) => t.headers ? Object.keys(t.headers).length : 0;

  const BTN: React.CSSProperties = {
    padding: "6px 14px", borderRadius: 8, fontSize: 13, fontWeight: 500,
    cursor: "pointer", border: `1px solid ${C.border}`, background: C.surface,
    color: C.text,
  };

  const SMALL_BTN: React.CSSProperties = {
    padding: "3px 8px", borderRadius: 6, fontSize: 11, fontWeight: 500,
    cursor: "pointer", border: `1px solid ${C.border}`, background: C.surface,
    color: C.textDim,
  };

  return (
    <SectionCard id="mcp" title="MCP Tools" active={active}>
      <div style={{ fontSize: 12.5, color: C.textDim, marginBottom: 14, lineHeight: 1.6 }}>
        Remote MCP tool endpoints the agent can call over HTTPS. Add public
        tools (HF Spaces, community servers) or authenticated tools (with custom headers).
        Each tool is synced to the active runtime and takes effect immediately.
      </div>

      {loading ? (
        <div style={{ fontSize: 12, color: C.textMuted }}>Loading…</div>
      ) : (
        <>
          {tools.length > 0 && (
            <div style={{ marginBottom: 16 }}>
              {tools.map((t) => (
                <div
                  key={t.name}
                  style={{
                    display: "flex", alignItems: "center", justifyContent: "space-between",
                    gap: 8, padding: "10px 12px", marginBottom: 6,
                    background: C.surface, border: `1px solid ${C.border}`, borderRadius: 8,
                  }}
                >
                  <div style={{ overflow: "hidden", minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: C.text, display: "flex", alignItems: "center", gap: 6 }}>
                      {t.name}
                      {headerCount(t) > 0 && (
                        <span style={{ fontSize: 10, color: C.amber, fontWeight: 400 }}>
                          {headerCount(t)} header{headerCount(t) > 1 ? "s" : ""}
                        </span>
                      )}
                    </div>
                    <div style={{
                      fontSize: 11, color: C.textMuted,
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {t.url}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleRemove(t.name)}
                    disabled={removing === t.name}
                    style={{ ...BTN, color: "#ef4444", flexShrink: 0, opacity: removing === t.name ? 0.5 : 1, cursor: removing === t.name ? "not-allowed" : "pointer" }}
                  >
                    {removing === t.name ? "Removing…" : "Remove"}
                  </button>
                </div>
              ))}
            </div>
          )}

          {tools.length === 0 && (
            <div style={{ fontSize: 12, color: C.textMuted, marginBottom: 14 }}>
              No MCP tools configured yet.
            </div>
          )}

          <div style={{ marginBottom: 6 }}>
            <label htmlFor="mcp-name" style={LABEL_STYLE}>Name</label>
            <input
              id="mcp-name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="search"
              style={INPUT_STYLE}
            />
          </div>
          <div style={{ marginBottom: 6 }}>
            <label htmlFor="mcp-url" style={LABEL_STYLE}>URL</label>
            <input
              id="mcp-url"
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://owner-space.hf.space/gradio_api/mcp/"
              style={INPUT_STYLE}
            />
          </div>

          <div style={{ marginBottom: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={LABEL_STYLE}>Headers <span style={{ color: C.textMuted, fontWeight: 400 }}>(optional)</span></span>
              <button type="button" onClick={addHeaderRow} style={SMALL_BTN}>+ Add</button>
            </div>
            {headers.map((h, i) => (
              <div key={i} style={{ display: "flex", gap: 6, marginBottom: 4 }}>
                <input
                  type="text"
                  value={h.key}
                  onChange={(e) => updateHeader(i, "key", e.target.value)}
                  placeholder="Authorization"
                  style={{ ...INPUT_STYLE, flex: 1 }}
                />
                <input
                  type="password"
                  value={h.value}
                  onChange={(e) => updateHeader(i, "value", e.target.value)}
                  placeholder="Bearer sk-..."
                  style={{ ...INPUT_STYLE, flex: 2 }}
                />
                <button type="button" onClick={() => removeHeaderRow(i)} style={{ ...SMALL_BTN, color: "#ef4444" }}>×</button>
              </div>
            ))}
          </div>

          <button
            type="button"
            onClick={handleAdd}
            disabled={adding || !name.trim() || !url.trim()}
            style={{
              ...BTN,
              background: C.amber, color: "#000", borderColor: C.amber,
              opacity: adding || !name.trim() || !url.trim() ? 0.5 : 1,
              cursor: adding ? "not-allowed" : "pointer",
            }}
          >
            {adding ? "Adding…" : "Add Tool"}
          </button>
        </>
      )}
    </SectionCard>
  );
}
