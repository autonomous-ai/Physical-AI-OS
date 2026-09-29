#!/usr/bin/env node
// Figma REST MCP server (stdio, Node stdlib only); token comes from FIGMA_TOKEN, never stored here.

import https from "node:https";
import readline from "node:readline";

const FIGMA_BASE = "https://api.figma.com/v1";
const SERVER_INFO = { name: "figma-rest", version: "1.0.0" };
const DEFAULT_PROTOCOL = "2025-03-26";

function token() {
  return (process.env.FIGMA_TOKEN || process.env.FIGMA_ACCESS_TOKEN || "").trim();
}

// authHeaderName: "Authorization" sends Bearer <token>; a custom name (e.g. X-Figma-Token) sends it raw.
function authHeaderName() {
  return (process.env.FIGMA_AUTH_HEADER || "Authorization").trim();
}

// ── Figma REST call ──
function figmaRequest(method, path, body) {
  return new Promise((resolve, reject) => {
    const url = new URL(FIGMA_BASE + path);
    const payload = body ? JSON.stringify(body) : null;
    const headers = {
      Accept: "application/json",
      "User-Agent": "lamp-figma-rest-mcp/1.0",
    };
    const hName = authHeaderName();
    if (hName.toLowerCase() === "authorization") {
      headers["Authorization"] = "Bearer " + token();
    } else {
      headers[hName] = token();
    }
    if (payload) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(payload);
    }
    const req = https.request(
      { method, hostname: url.hostname, path: url.pathname + url.search, headers },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          let json;
          try {
            json = raw ? JSON.parse(raw) : null;
          } catch {
            json = undefined;
          }
          resolve({ status: res.statusCode, json, text: raw });
        });
      },
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
  }
  return parts.length ? "?" + parts.join("&") : "";
}

// ── Tool definitions ──
const STR = (description) => ({ type: "string", description });

const TOOLS = [
  {
    name: "figma_get_me",
    description: "Get the authenticated Figma user (id, email, handle).",
    inputSchema: { type: "object", properties: {} },
    run: () => figmaRequest("GET", "/me"),
  },
  {
    name: "figma_get_file",
    description:
      "Get a Figma file's document tree (pages, frames, nodes). Use `depth` to limit nesting (e.g. 2 = pages + their top-level frames) and avoid huge payloads.",
    inputSchema: {
      type: "object",
      properties: {
        key: STR("File key from the Figma URL: figma.com/design/<KEY>/..."),
        depth: { type: "number", description: "Tree depth to return (optional)." },
      },
      required: ["key"],
    },
    run: (a) => figmaRequest("GET", `/files/${encodeURIComponent(a.key)}${qs({ depth: a.depth })}`),
  },
  {
    name: "figma_get_file_nodes",
    description: "Get specific nodes from a file by id (comma-separated ids like '1:23,4:56').",
    inputSchema: {
      type: "object",
      properties: {
        key: STR("File key."),
        ids: STR("Comma-separated node ids."),
        depth: { type: "number", description: "Tree depth (optional)." },
      },
      required: ["key", "ids"],
    },
    run: (a) =>
      figmaRequest("GET", `/files/${encodeURIComponent(a.key)}/nodes${qs({ ids: a.ids, depth: a.depth })}`),
  },
  {
    name: "figma_get_file_meta",
    description: "Get lightweight file metadata (name, lastModified, editorType, role) without the node tree.",
    inputSchema: {
      type: "object",
      properties: { key: STR("File key.") },
      required: ["key"],
    },
    run: (a) => figmaRequest("GET", `/files/${encodeURIComponent(a.key)}/meta`),
  },
  {
    name: "figma_get_file_versions",
    description: "Get the version history of a file.",
    inputSchema: {
      type: "object",
      properties: { key: STR("File key.") },
      required: ["key"],
    },
    run: (a) => figmaRequest("GET", `/files/${encodeURIComponent(a.key)}/versions`),
  },
  {
    name: "figma_get_images",
    description:
      "Render nodes to image URLs. Returns a map of node id -> image URL. format: png|svg|pdf|jpg.",
    inputSchema: {
      type: "object",
      properties: {
        key: STR("File key."),
        ids: STR("Comma-separated node ids to render."),
        format: STR("png | svg | pdf | jpg (default png)."),
        scale: { type: "number", description: "Scale 0.01–4 (raster only)." },
      },
      required: ["key", "ids"],
    },
    run: (a) =>
      figmaRequest(
        "GET",
        `/images/${encodeURIComponent(a.key)}${qs({ ids: a.ids, format: a.format, scale: a.scale })}`,
      ),
  },
  {
    name: "figma_get_comments",
    description: "Read the comments on a file.",
    inputSchema: {
      type: "object",
      properties: { key: STR("File key.") },
      required: ["key"],
    },
    run: (a) => figmaRequest("GET", `/files/${encodeURIComponent(a.key)}/comments`),
  },
  {
    name: "figma_post_comment",
    description: "Post a comment on a file (optionally as a reply to comment_id).",
    inputSchema: {
      type: "object",
      properties: {
        key: STR("File key."),
        message: STR("Comment text."),
        comment_id: STR("Parent comment id to reply to (optional)."),
      },
      required: ["key", "message"],
    },
    run: (a) =>
      figmaRequest("POST", `/files/${encodeURIComponent(a.key)}/comments`, {
        message: a.message,
        ...(a.comment_id ? { comment_id: a.comment_id } : {}),
      }),
  },
];

const TOOL_BY_NAME = new Map(TOOLS.map((t) => [t.name, t]));

// ── JSON-RPC plumbing ──
function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

function reply(id, result) {
  send({ jsonrpc: "2.0", id, result });
}

function replyError(id, code, message) {
  send({ jsonrpc: "2.0", id, error: { code, message } });
}

async function handleToolCall(id, params) {
  const name = params?.name;
  const args = params?.arguments || {};
  const tool = TOOL_BY_NAME.get(name);
  if (!tool) {
    return replyError(id, -32602, `Unknown tool: ${name}`);
  }
  if (!token()) {
    return reply(id, {
      content: [{ type: "text", text: "Error: FIGMA_TOKEN (or FIGMA_ACCESS_TOKEN) is not set on the MCP server." }],
      isError: true,
    });
  }
  try {
    const res = await tool.run(args);
    const ok = res.status >= 200 && res.status < 300;
    const bodyText =
      res.json !== undefined ? JSON.stringify(res.json, null, 2) : String(res.text || "");
    const text = ok ? bodyText : `Figma API HTTP ${res.status}:\n${bodyText}`;
    reply(id, { content: [{ type: "text", text }], isError: !ok });
  } catch (e) {
    reply(id, { content: [{ type: "text", text: `Request failed: ${e?.message || e}` }], isError: true });
  }
}

function handle(msg) {
  if (msg.id === undefined || msg.id === null) return;
  const { id, method, params } = msg;
  switch (method) {
    case "initialize":
      return reply(id, {
        protocolVersion: params?.protocolVersion || DEFAULT_PROTOCOL,
        capabilities: { tools: {} },
        serverInfo: SERVER_INFO,
      });
    case "ping":
      return reply(id, {});
    case "tools/list":
      return reply(id, {
        tools: TOOLS.map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
      });
    case "tools/call":
      return void handleToolCall(id, params);
    default:
      return replyError(id, -32601, `Method not found: ${method}`);
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  const s = line.trim();
  if (!s) return;
  let msg;
  try {
    msg = JSON.parse(s);
  } catch {
    return;
  }
  try {
    handle(msg);
  } catch (e) {
    if (msg && msg.id !== undefined && msg.id !== null) {
      replyError(msg.id, -32603, `Internal error: ${e?.message || e}`);
    }
  }
});
rl.on("close", () => process.exit(0));
