// Local-only native skill eligibility adapter. No credentials or network I/O.
import { open, realpath } from "node:fs/promises";
import { constants } from "node:fs";
import path from "node:path";

export async function boundedRead(file, limit) {
  const handle = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > limit) throw new Error("file_size");
    const bytes = Buffer.alloc(limit + 1); let size = 0;
    while (size < bytes.length) {
      const { bytesRead } = await handle.read(bytes, size, bytes.length-size, null);
      if (!bytesRead) break;
      size += bytesRead;
    }
    if (size > limit) throw new Error("file_size");
    return bytes.subarray(0, size).toString("utf8");
  } finally { await handle.close(); }
}

function toolPolicyAllows(tools) {
  // These are the OS defaults. Custom tool policies remain native-owned.
  return !tools || (Object.keys(tools).every(k => ["profile", "exec", "elevated"].includes(k)) && (!tools.profile || tools.profile === "full"));
}

export function policyAllows(config, ctx) {
  const id = "autonomous-jev", plugin = config.plugins?.entries?.[id];
  if (plugin?.config?.enabled !== true) return false;
  if (config.plugins?.enabled === false || plugin?.enabled === false || plugin?.config?.enabled === false || config.plugins?.deny?.includes(id)) return false;
  if (Array.isArray(config.plugins?.allow) && !config.plugins.allow.includes(id)) return false;
  const agent = config.agents?.list?.find(a => a.id === ctx.agentId);
  const sandbox = agent?.sandbox ?? config.agents?.defaults?.sandbox;
  if (sandbox && sandbox.mode !== "off") return false;
  if (!toolPolicyAllows(config.tools) || !toolPolicyAllows(agent?.tools)) return false;
  const hasTools = obj => obj && typeof obj === "object" && (Object.hasOwn(obj, "tools") || Object.values(obj).some(hasTools));
  return !hasTools(config.channels);
}

export async function nativeCatalog(api, ctx) {
  const config = api.runtime.config.current?.() ?? api.runtime.config.loadConfig?.();
  if (!config || !ctx.sessionKey || !ctx.sessionId || !ctx.workspaceDir || !policyAllows(config, ctx)) return [];
  let entry;
  if (api.runtime.agent?.session?.getSessionEntry) {
    entry = api.runtime.agent.session.getSessionEntry({ sessionKey: ctx.sessionKey, agentId: ctx.agentId, readConsistency: "latest", hydrateSkillPromptRefs: true });
  } else {
    const resolve = api.runtime.channel?.session?.resolveStorePath;
    if (!resolve) return [];
    entry = JSON.parse(await boundedRead(resolve(config.session?.store, { agentId: ctx.agentId }), 4 * 1024 * 1024))[ctx.sessionKey];
  }
  const snapshot = entry?.sessionId === ctx.sessionId ? entry.skillsSnapshot : null;
  if (!Array.isArray(snapshot?.resolvedSkills) || typeof snapshot.prompt !== "string") return [];
  const agent = config.agents?.list?.find(a => a.id === ctx.agentId);
  const skills = [], seen = new Set();
  for (const skill of snapshot.resolvedSkills) {
    if (!/^[a-zA-Z0-9_.-]{1,128}$/.test(skill.name) || seen.has(skill.name) || typeof skill.description !== "string" || !skill.description.trim() || skill.disableModelInvocation ||
        !snapshot.prompt.includes(`<name>${skill.name}</name>`) || config.skills?.entries?.[skill.name]?.enabled === false ||
        (Array.isArray(agent?.skills) && !agent.skills.includes(skill.name)) ||
        (Array.isArray(snapshot.skillFilter) && !snapshot.skillFilter.includes(skill.name))) continue;
    seen.add(skill.name);
    // The native snapshot is the authority for bundled, managed, plugin and
    // workspace locations. Never discover paths independently or follow aliases.
    if (typeof skill.filePath !== "string" || !path.isAbsolute(skill.filePath)) continue;
    let file, content;
    try {
      file = await realpath(skill.filePath);
      if (file !== path.resolve(skill.filePath) || path.basename(file) !== "SKILL.md") continue;
      content = await boundedRead(file, 32768);
    } catch { continue; } // A removed or unreadable skill must not hide the rest.
    const front = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(content)?.[1];
    if (!front) continue;
    // Accept only simple metadata; native handles dependency/platform/permission declarations.
    const keys = [...front.matchAll(/^([^\s:#][^:]*):/gm)].map(m => m[1].trim());
    const name = /^name:\s*["']?([a-zA-Z0-9_.-]+)["']?\s*$/m.exec(front)?.[1];
    if (name !== skill.name || keys.some(k => !["name", "description", "license", "compatibility"].includes(k)) || /[&*!]/.test(front)) continue;
    skills.push({ name: skill.name, description: skill.description, file, content });
  }
  return skills.sort((a, b) => a.name.localeCompare(b.name));
}
