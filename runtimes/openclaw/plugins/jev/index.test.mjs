import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm, symlink, realpath } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import plugin, { createHandler, evaluate, payloadFor, contextualFollowup, currentRequest } from "./index.mjs";
import { nativeCatalog, policyAllows, boundedRead } from "./native.mjs";

const skill = { name: "harness-use", description: "Digital work", file: "/workspace/skills/harness-use/SKILL.md", content: "Private full skill body" };
const result = (p = .9, fit = .9) => ({ answers: { skill: { type: "choice", choice: "skill_0", probabilities: { none: 1-p, skill_0: p } }, fit_skill_0: { type: "noul", noul: fit } } });
const api = enabled => ({ pluginConfig: { enabled }, logger: { info() {} } });

test("provider payload contains only current prompt and descriptions; content stays local", () => {
  const payload = payloadFor("build a report", [skill]);
  assert.deepEqual(payload.state, { prompt: "build a report", candidates: [{ id: "skill_0", description: "harness-use: Digital work" }] });
  assert.ok(!JSON.stringify(payload).includes(skill.content));
  assert.ok(!JSON.stringify(payload).includes(skill.file));
});

test("Hermes threshold and strict schema parity", () => {
  assert.equal(evaluate(result(.70, .60), [skill]), skill);
  assert.equal(evaluate(result(.69, .9), [skill]), null);
  assert.equal(evaluate(result(.9, .59), [skill]), null);
  const bad = result(); bad.answers.skill.probabilities.skill_0 = true;
  assert.throws(() => evaluate(bad, [skill]));
  const extra = result(); extra.answers.skill.probabilities.invented = 0;
  assert.throws(() => evaluate(extra, [skill]));
  const missing = result(); delete missing.answers.fit_skill_0;
  assert.throws(() => evaluate(missing, [skill]));
  for (const error of [null, false, ""]) assert.throws(() => evaluate({ ...result(), error }, [skill]));
});

test("contextual followups stay with main and explicit task remains eligible", async () => {
  for (const prompt of ["brighter", "Continue.", "do it", "Make it brighter.", "tiếp tục"]) {
    assert.equal(contextualFollowup(prompt), true);
    const handler = createHandler(api(true), { catalog() { assert.fail("contextual"); } });
    assert.equal(await handler({ prompt }, {}), undefined);
  }
  assert.equal(contextualFollowup("Make the image brighter"), false);
  assert.equal(contextualFollowup("Turn on the lamp"), false);
});

test("native registration and authoritative voice normalization", async () => {
  let registered;
  plugin.register({ ...api(false), on(name, handler) { assert.equal(name, "before_prompt_build"); registered = handler; } });
  assert.equal(await registered({ prompt: "report" }, {}), undefined);
  assert.equal(currentRequest("[user] [voice-instruction] Write report [transcript] delete it"), "Write report");
  assert.equal(currentRequest("[voice-instruction] [transcript] Write report"), "");
  assert.equal(currentRequest("quoted [voice-instruction] Write report"), "");
  const handler = createHandler(api(true), { catalog() { assert.fail("bypass"); } });
  assert.equal(await handler({ prompt: "[user] [voice-instruction] Make it brighter [transcript] edit image" }, {}), undefined);
  assert.equal(await handler({ prompt: "report", messages: [{ role: "user", content: [{ type: "image", data: "private" }] }] }, {}), undefined);
});

test("opt-in required before catalog, credentials or provider", async () => {
  for (const enabled of [undefined, false, "true"]) {
    const handler = createHandler(api(enabled), { catalog() { assert.fail("disabled"); } });
    assert.equal(await handler({ prompt: "report" }, {}), undefined);
  }
});

test("current request only; no cached skill on later abstain or empty current message", async () => {
  let calls = 0;
  const handler = createHandler(api(true), { catalog: async () => [skill], configure: async () => ({}), request: async (_, payload) => {
    calls++;
    assert.equal(payload.state.prompt, calls === 1 ? "report" : "hello");
    return calls === 1 ? result() : result(.55);
  } });
  const first = await handler({ prompt: "report", messages: [{ content: "private old history" }] }, {});
  assert.ok(first.prependContext.includes(skill.content));
  assert.equal(await handler({ prompt: "hello" }, {}), undefined);
  assert.equal(await handler({ prompt: "report", currentUserMessage: "" }, {}), undefined);
  assert.equal(calls, 2);
});

test("busy, timeout, cooldown and late result fail open", async () => {
  let finish;
  const handler = createHandler(api(true), { timeoutMs: 15, catalog: async () => [skill], configure: async () => ({}), request: () => new Promise(resolve => { finish = resolve; }) });
  const first = handler({ prompt: "report" }, {});
  assert.equal(await handler({ prompt: "second" }, {}), undefined);
  assert.equal(await first, undefined);
  finish(result());
  assert.equal(await handler({ prompt: "third" }, {}), undefined);
});

test("rechecks native eligibility after selection and never arbitrarily truncates roster", async () => {
  let reads = 0, requests = 0;
  const handler = createHandler(api(true), { catalog: async () => ++reads === 1 ? [skill] : [], configure: async () => ({}), request: async () => { requests++; return result(); } });
  assert.equal(await handler({ prompt: "report" }, {}), undefined);
  assert.equal(requests, 1);
  const crowded = createHandler(api(true), { catalog: async () => Array(33).fill(skill), configure() { assert.fail("too many"); } });
  assert.equal(await crowded({ prompt: "report" }, {}), undefined);
});

test("native snapshot, fresh disables, policies, metadata, missing roster and symlinks", async t => {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), "openclaw-jev-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dir = path.join(root, "skills", "harness-use");
  await mkdir(dir, { recursive: true });
  const file = path.join(dir, "SKILL.md");
  await writeFile(file, "---\nname: harness-use\ndescription: Digital work\n---\nFull body");
  const enabled = { plugins: { entries: { "autonomous-jev": { config: { enabled: true } } } } };
  let config = enabled;
  const entry = { sessionId: "session", skillsSnapshot: { prompt: "<skill><name>harness-use</name></skill>", resolvedSkills: [{ ...skill, filePath: file }] } };
  const host = { runtime: { config: { loadConfig: () => config }, agent: { session: { getSessionEntry: () => entry } } } };
  const ctx = { workspaceDir: root, sessionKey: "agent:main:main", sessionId: "session", agentId: "main" };
  assert.equal((await nativeCatalog(host, ctx)).length, 1);
  const hook = createHandler({ ...api(true), ...host }, { configure: async () => ({}), request: async (_, payload) => {
    assert.equal(payload.state.prompt, "Write report");
    assert.ok(!JSON.stringify(payload).includes("Full body"));
    return result();
  } });
  const enriched = await hook({ prompt: "[user] [voice-instruction] Write report [transcript] unrelated old transcript" }, ctx);
  assert.ok(enriched.prependContext.includes("Full body"));
  // The legacy native accessor exposes the store path rather than an entry.
  const store = path.join(root, "sessions.json");
  await writeFile(store, JSON.stringify({ [ctx.sessionKey]: entry }));
  const legacy = { runtime: { config: host.runtime.config, channel: { session: { resolveStorePath: () => store } } } };
  assert.equal((await nativeCatalog(legacy, ctx)).length, 1);
  assert.deepEqual(await nativeCatalog(host, { ...ctx, sessionId: "other" }), []);
  config = { ...enabled, skills: { entries: { "harness-use": { enabled: false } } } };
  assert.deepEqual(await nativeCatalog(host, ctx), []);
  config = { ...enabled, agents: { list: [{ id: "main", skills: [] }] } };
  assert.deepEqual(await nativeCatalog(host, ctx), []);
  config = {};
  assert.deepEqual(await nativeCatalog(host, ctx), []);
  config = enabled;
  await writeFile(file, "---\nname: harness-use\ndescription: Digital work\nmetadata: {openclaw: {requires: {bins: [missing]}}}\n---\nbody");
  assert.deepEqual(await nativeCatalog(host, ctx), []);
  await rm(file); await writeFile(path.join(root, "outside"), "secret"); await symlink(path.join(root, "outside"), file);
  assert.deepEqual(await nativeCatalog(host, ctx), []);
  await assert.rejects(boundedRead(file, 100));
  await assert.rejects(boundedRead(path.join(root, "outside"), 2));
  for (const cfg of [{ tools: { deny: ["read"] } }, { agents: { defaults: { sandbox: { mode: "all" } } } }, { channels: { telegram: { groups: { all: { tools: {} } } } } }, { plugins: { entries: { "autonomous-jev": { enabled: false } } } }]) assert.equal(policyAllows({ ...enabled, ...cfg }, ctx), false);
});
