# Adding an Agentic Runtime Backend

How to add a new agent "brain" (like OpenClaw, Hermes, PicoClaw) to the OS, and
**everything it must wire up** so it reaches parity. Written from the lessons of
the Hermes port, where too many pieces were left as silent no-ops and bit us
later (config that didn't survive a factory reset, skills that never came back,
identity rename that did nothing, no skill auto-update).

> **The one rule to remember:** the OS (`os-server`) is the platform; the backend
> is a swappable brain. Anything user-visible that OpenClaw does, a new backend
> must either do too or **consciously decide to skip — and say why in a comment.**
> A no-op is a decision, never a default.

Source of truth for the contract: `system/domain/agent.go` (the
`AgentGateway` interface). This doc explains *which* parts matter and *how* to
wire the switch, install, migration, skills, hooks, and reset.

> **Agentic-backend docs:** this file (generic contract + how to add one) ·
> [`hermes.md`](hermes.md) (Hermes, a full backend) · [`picoclaw.md`](picoclaw.md)
> (PicoClaw, client-only gateway with install/presync scripts) ·
> [`claudecode.md`](claudecode.md) (Claude Code behind a local bridge, native
> channel plugins for Telegram/Discord, claude.ai OAuth login). Per-backend
> protocol/quirks live in those; the generic mechanics + checklist live here.

---

## 0. Mental model

- `config.agent_runtime` (`/root/config/config.json`) selects the active backend.
- `system/agent/factory.go` `ProvideGateway` resolves it at boot via Wire DI:
  `config.agent_runtime` > ROBOT.md `gateway.default` > openclaw.
- **Seed-on-empty:** at boot `device.ProvideService` calls
  `SeedAgentRuntimeFromGateway` — when `config.agent_runtime` is empty/null **and**
  ROBOT.md `gateway.default` names a valid runtime, that value is written into
  config.json (idempotent; only the first boot of a fresh/legacy config writes).
  Once a concrete value is on disk the device **owns** its runtime: a dev who set
  it (via switch or by hand) is left untouched, and the resolve-fallback above
  becomes a no-op. Corollary: changing `gateway.default` in ROBOT.md afterwards no
  longer affects an already-seeded device — edit config.json or switch the runtime.
- Switching at runtime goes through one core — `device.Service.UpdateAgentRuntime`
  — fired by 3 triggers (MQTT `<runtime>.setup`, HTTP `/api/device/agent-runtime`,
  web Runtime section). See `docs/agentic/hermes.md` §10–§11.
- **Completion signaling:** MQTT acks three phases per `<runtime>.setup` —
  `starting` immediately, then `success` (published *before* the os-server
  restart) or `failure` (+error, switch-runtime already rolled back); the `info`
  uplink carries `agent_runtime` + per-runtime versions for re-confirmation.
  HTTP `POST /api/device/agent-runtime` returns 200 = *accepted only* (the
  switch runs in the background); the truth is `GET /api/device/agent-runtime`,
  because `config.agent_runtime` is persisted only after the switch lands. The
  web Runtime section polls that GET after the POST until the target runtime is
  reported (success) or a 5-minute deadline passes (shows the actually-active
  runtime instead of the optimistic one).

---

## 1. The contract — implement `domain.AgentGateway`

Your backend lives in `runtimes/<name>/` and its `*Service` must satisfy the
**whole** `AgentGateway` interface. The methods fall into groups:

| Group | Examples | New-backend stance |
|-------|----------|--------------------|
| **Core turn** | `SendChatMessage`, `SendSystemChatMessage`, `*WithImage`, `NextChatRunID`, `*WithRun`, `StartWS` | **MUST** — this is the agent. |
| **Readiness / busy** | `IsReady`, `ConnectedAt`, `AgentUptime`, `IsBusy`, `SetBusy`, `QueuePendingEvent` | **MUST** — os-server gates sensing on these. |
| **Identity** | `Name`, `Version`, `GetSessionKey`/`SetSessionKey` | **MUST** — surfaces in web Status. |
| **HAL passthrough** | `SendToHALTTS*`, `StopTTS`, `SetVolume`, `StartHALVoice` | Usually identical across backends — share or copy. |
| **Run markers** | `MarkGuardRun`/`Consume*`, `MarkBroadcastRun`, `MarkPoseBucketRun`, `MarkWebChatRun`, `MarkSilentRun`, `*PendingChatTrace*` | **MUST track** — os-server tags turns by runID; these are runtime-shaped but the OS depends on them. |
| **Channels** | `SupportedChannels`, `AddChannel`, `RefreshChannelConfig`, `PairWhatsapp`, `HasWhatsappSession`, `GetTelegram*`, `Broadcast`, `SendToUser*` | `SupportedChannels()` declares real capability; `AddChannel`/`RefreshChannelConfig` **MUST** return `domain.ErrChannelNotSupported` for anything outside that list (no more silent no-op). See §9. |
| **Lifecycle / onboarding** | `SetupAgent`, `EnsureOnboarding`, `ResetAgent`, `RestartAgent`, `RefreshModelsConfig` | Decide per backend; document the no-op. |
| **Migration-adjacent** | `UpdateIdentityName`, `StartSkillWatcher`, `WatchIdentity`, `StartModelSync`, `UpdatePrimaryModel`, `StartPrimaryModelWatch`, `CompactSession`, `NewSession`, `FetchChatHistory`, `WriteMCPEntry`/`RemoveMCPEntry`, `GetConfigJSON` | **The danger zone** — easy to no-op, expensive to discover missing. See §4–§6. |

**Lesson (Hermes):** ~15 of these were stubbed no-op in `runtimes/hermes/stubs.go`.
Some are legitimately N/A (`PairWhatsapp` — no plugin); `WriteMCPEntry`/`RemoveMCPEntry`
were initially deferred (`TODO(hermes-mcp)`) and **later implemented** against
`config.yaml` `mcp_servers` (`runtimes/hermes/mcp.go`), with a config→config clone on
runtime switch (`system/agent/mcp_reconcile.go`). But `StartSkillWatcher`,
`UpdateIdentityName`, and the config-sync path
were **functional gaps**, not N/A — they shipped as no-ops and we only noticed when
skills went stale / rename did nothing / config broke after a reset. Audit every
stub: write `// no-op because <reason>` or `// TODO(<backend>-<feature>)`, never a
bare empty body.

**No fake success.** A legitimately-N/A method that returns an error **must
return `domain.ErrNotSupportedByRuntime`** (domain/agent.go), never `nil` —
`nil` tells the caller the change was applied when nothing happened. Callers
branch on `errors.Is`: the LLM model/baseURL save path
(`system/device/config_update.go`) logs it as informational and falls back to
`EnsureOnboarding` (whose presync re-reads `llm_*` from config.json) — on **both**
branches, the model-only one included. That branch used to only log, which was
survivable while these runtimes pinned a fixed model alias; once a device could
run the operator's own model, a model-only save sat unapplied until something
else restarted the gateway. The sentinel means "not patchable in place", never
"nothing to do". The
gw-config endpoint reports "no device-side config file" instead of `{}`.
Current sentinel-returning stubs: `UpdatePrimaryModel`, `RefreshModelsConfig`,
`CompactSession` (hermes + picoclaw), `GetConfigJSON` (hermes only — picoclaw
has a real config.json). Mirrors the `ErrChannelNotSupported` rule in §9.

---

## 2. Register + wire the switch

1. `domain/device.go`: add `AgentRuntime<Name>` const + the entry in `AgentRuntimes`.
2. `system/agent/factory.go`: add the `case` in `ProvideGateway`.
3. **Embedded installer**: `runtimes/<name>/install.sh` + `install.go`
   (`//go:embed install.sh` → `runtimereg.Register(name, InstallScript)`).
4. `switch_runtime.sh` is **generic** — it knows no backend names. Do **not** edit
   it, the imager, or os-server's switch core to add a backend.

The installer contract (`switch_runtime.sh` expects):
- create a systemd unit; declare its name in `/usr/local/lib/os-runtimes/<name>/service`
  if it isn't `<name>.service`.
- optionally drop a `verify` hook at `/usr/local/lib/os-runtimes/<name>/verify`
  (exit 0 = "installed & usable"). Keep it **cheap** — see §3 for why it must not
  over-check.

### The switch flow (what happens on a switch)

`device.Service.UpdateAgentRuntime` validates the runtime, captures the active
`old`, then runs the switcher under `systemd-run --wait` and **blocks on its exit
code**. `config.agent_runtime` is persisted **only after a clean exit 0** — so a
crash/reboot mid-switch resolves the still-installed `old`, and there is nothing to
revert. `switch-runtime <new> <old>` (generic, `system/device/switch_runtime.sh`,
`go:embed`-materialized to `/usr/local/bin/switch-runtime`):

1. resolves `<new>`'s unit name (default `<new>.service`, or the name declared in
   `/usr/local/lib/os-runtimes/<new>/service`) and checks **installed AND usable** —
   unit present **and** the `verify` hook passes (no verify hook → unit-presence
   alone). If not, runs the installer (embedded copy first, CDN fallback). This
   closes the orphaned-unit trap (a stale `.service` whose binary is gone).
2. runs `/usr/local/bin/runtime-<new>-presync` (materialized by os-server — §3).
3. `enable --now <new-unit>` **and asserts it actually reached active** (a unit can
   enable cleanly yet crash on a missing binary). If it didn't start and the
   installer hadn't run this pass, it **reinstalls once and retries**. Then stops
   the old unit (up to 3 `disable --now` retries).
4. exits 0 — it does **not** restart os-server (os-server, blocked on `--wait`, acks
   the real outcome then restarts itself so `factory.go` re-resolves the gateway).
   On failure a rollback trap restarts only the **old** unit. It never touches
   `config.json`.

So `switch-runtime` is fully backend-agnostic — **no imager / setup.sh / switcher
change is ever needed to add a backend.**

---

## 3. The golden rule: install-once vs every-switch (the *activation gap*)

`install.sh` runs **once** — `switch_runtime.sh` only runs it on a first install or
when `verify` fails. On every later switch it is **skipped**.

> **Therefore: anything that must survive a factory reset, or must refresh on a
> plain `os-server` OTA, MUST NOT be written only by `install.sh`. It must be
> materialized by `os-server` on every switch.**

This is the **activation gap** and we hit it twice on Hermes:
- A fix shipped inside `install.sh` (or any file `install.sh` writes — the
  `verify` hook, the presync hook) **never reaches an already-installed device**
  via OTA: the old on-disk copy keeps passing `verify`, so `install.sh` never
  re-runs, so the new copy never lands.

The fix pattern (use it for everything stateful):
- Put the logic in a **presync hook** (`runtime-<name>-presync`).
- Embed it: `//go:embed presync.sh` → `runtimereg.RegisterPresync(name, PresyncScript)`.
- `os-server` materializes it every switch (`system/device/runtime_installers.go`
  `materializePresync`, called from the switch flow next to `materializeInstaller`).
- `switch_runtime.sh` runs `runtime-<name>-presync` right before the backend starts.

Hermes's presync (`runtimes/hermes/presync.sh`) now owns **both** the
`config.yaml` model wiring (idempotent — coerces a reset-blanked `model: ''` back
to a map, asserts `provider`/`custom_providers` structure, syncs `llm_*`/secrets)
**and** the skill restore (re-runs `claw migrate` when `skills/openclaw-imports`
is empty). Keep `verify` CLI-only (`command -v <bin>`) — a structure-check in
`verify` would force a heavy full reinstall when presync alone heals it.

---

## 4. Persona + memory migration (Go, runs every switch)

**Hub-and-spoke, not per-pair.** Migration goes through a runtime-neutral
`PersonaBundle`: each runtime has ONE **read** adapter (its on-disk layout →
bundle) and ONE **write** adapter (bundle → its layout), in
`system/agent/migrate_persona/runtime_<name>.go`. A migration is
`read[from] → write[to]` (`RunMigration(from, to, opts)`). So adding a runtime is
**one adapter file** that interoperates with every existing runtime in both
directions — file count is **linear (2 per runtime)**, not the quadratic N×(N-1)
a per-pair migrator needs. Register the adapter in the `adapters` map in
`migrator.go`; nothing else changes (no new `Direction` enum). openclaw, hermes,
picoclaw, codex, claudecode, and opencode all have adapters, so any pair migrates both
ways. A runtime with no registered adapter is skipped by `CanMigrate` — the boot-time
reconciler doesn't migrate to/from it.

The `runtimeAdapter` interface (`migrator.go`) also carries two methods that are
not about migration content, but that every adapter must still implement because
other subsystems key off the same interface:

- `memoryFilePath(opts)` — where the runtime keeps the `MEMORY.md` it loads every
  session. The OS memory guard (`docs/os-server.md`, "Memory guard") sweeps it
  together with `userProfilePath(opts)` at boot and on every write; a runtime
  without it is unguarded, which is why it is on the interface and not a table.
- `workspaceRoot(opts)` — the directory HAL treats as the runtime's workspace
  (`<root>/realtime/` holds `summary.md` etc.). `POST /api/agent/memory/reset`
  backs up and clears memory under it for every installed runtime.

PicoClaw's adapter (`runtime_picoclaw.go`) mirrors openclaw's layout but reads/writes
`memory/MEMORY.md` (picoclaw keeps it under `memory/`, not at the workspace root).
Note its INBOUND skills still come from presync's `picoclaw migrate --workspace-only`
(`picoclaw.md` §1.1) — the Go reconciler only carries persona/memory, not skills — so
on a switch INTO picoclaw the two overlap on persona (same source, harmless) while
presync remains the only skills path.

> **Copy-me template:** `system/agent/migrate_persona/runtime_example.go` is a
> build-ignored, fully-annotated skeleton — copy it to `runtime_<name>.go`, delete
> the `//go:build ignore` line, and fill in the 5 wiring steps + read/write. It
> spells out the per-field decision (separate slot vs inline vs fold) inline.

Migration runs at os-server boot after a real switch (`Reconcile`, when
`agent_state.json` prev ≠ current). What each adapter carries:

- **SOUL.md** → backend's identity file. If the backend has **no separate
  IDENTITY.md slot** (Hermes doesn't), inline the owner's filled IDENTITY fields
  as a `## Your identity card` block in SOUL (see `buildIdentityBlock`).
- **MEMORY.md + `memory/*.md` daily + KNOWLEDGE.md** → merged into the backend's
  long-term memory file. **First check which files the backend LOADS BY NAME** —
  Hermes loads only `MEMORY.md` + `USER.md` (no `memories/*.md` glob), so a
  separate `KNOWLEDGE.md` would be ignored; we fold it into `MEMORY.md` instead.
- **USER.md** → backend's user-profile file, via `writeUserProfile` (NOT
  `writeMemoryEntries`). USER.md is half record, half form: its SINGULAR fields
  (`Name`, `What to call them`, `Pronouns`, `Timezone`) are **replace-or-append**
  — one bullet each, incoming wins — while everything else keeps the additive
  dedupe-union. Entry-merge alone treats `**Name:** Leo` and `**Name:** Long` as
  two different strings, so a profile could gain a name but never retire one, and
  every switch propagated the pair (device-observed 2026-09-03). Two invariants
  the tests pin:
  - An absent or unfilled incoming field **never blanks** a filled destination —
    a source with no profile yet must not erase what the device already learned.
  - Only the FIELD is retired. Free-form prose that mentions an old user is
    ordinary learned content and stays; removing that is the enrollment-keyed
    prune's job, not migration's.
  Treat USER.md as a **form, not a log**. Its template ships blank slots
  (`- **Name:**`) under an instruction to fill them in, so `writeUserProfile`
  fills the slot **where it stands** and drops any later duplicate bullet for the
  same field — the same move `setIdentityField` makes for IDENTITY.md. The whole
  template survives verbatim: the instruction, the Context prompts, the "not
  building a dossier" guardrail, still-blank slots, even the `## Related` link.
  USER.md is a bootstrap file, so all of that reaches the agent every turn and it
  carries the only line telling the agent to maintain the file at all. Do not
  "clean up" the template — the migration's job is the values, not the form.
- Set **`Overwrite = true`** for the soul copy on a switch: a switch means "adopt
  the persona I was just using." `copyPersona` backs up first (`.bak-<nano>`).
- The reverse direction must **strip backend-only artifacts** it added (e.g. the
  identity card — OpenClaw keeps the name in its own IDENTITY.md) **and restore
  what the forward inlined back into its native slot**. Inlining the name into the
  Hermes SOUL but only stripping it on the way back **loses the name** — the reverse
  must parse the card and write the fields into the OpenClaw `IDENTITY.md`
  (`restoreIdentityCard`, the inverse of `ensureIdentityInlined`: line
  replace-or-append, preserving the existing template; relies on the same
  "onboard does not clobber an existing IDENTITY.md" guarantee `UpdateIdentityName`
  uses). Every forward inline needs a matching reverse restore, or round-tripping
  silently drops state.

Do **NOT** carry runtime-specific files: `AGENTS.md`, `TOOLS.md`, `HEARTBEAT.md`,
`hooks/` — they belong to the source runtime. The backend's **deep memory engine**
(episodic/semantic DB, dream-diary, grounded-short-term) is **not portable** —
the distilled `MEMORY.md`/`USER.md` is the portable form.

### Classify every migration step: fold vs move

Whether a switch round-trips losslessly is **per-backend**, decided by which slots
the target backend has. Don't assume symmetry — classify each forward step:

- **Move / inline** (a field goes to a *different* slot, e.g. IDENTITY name →
  inlined into SOUL): reversible, and the reverse **MUST** restore it to its native
  slot. Skipping the restore silently drops state — that was the identity-name bug.
  **Every inline needs a matching reverse restore.**
- **Fold** (two source structures collapse into *one* target, because the target
  backend lacks a slot for one of them): inherently lossy on **structure** (not on
  data), and has **no faithful inverse** — once merged, the entries can't be split
  back apart. Only do a fold when the target genuinely lacks the slot, and document
  it as a known one-way asymmetry **in that backend's own doc** (it is a property of
  that backend, not of the migration framework — another backend that *has* the slot
  would map 1:1 and round-trip cleanly).

So a backend's migration symmetry is a fact about *that backend's slots*, recorded
in its backend doc (e.g. `docs/agentic/hermes.md`), not a blanket guarantee here.

---

## 5. Skills

- Skills reach the backend by being **copied** (verify: copy vs convert! Hermes's
  `claw migrate` is `shutil.copytree`, no transform) into the backend's skill dir.
- **Restore-after-reset** belongs in **presync**, guarded on the dir being empty
  (so a normal switch is a no-op — no churn). See §3.
- **Skill watcher** (auto-update from CDN, capability-gated): the generic
  fetch/extract/hash plumbing is shared in `system/skills/skillzip.go`
  (`FetchSkillVersions`/`DownloadToTempFile`/`FolderHash`/`ExtractSkillZip`).
  Build the reload message with `skills.SkillUpdatePrompt(agentVisibleSkillsDir, changedSkills)`
  from `system/skills/skill_update_prompt.go`; all six runtimes share its wording and
  list formatting. Keep the runtime-specific directory, empty-change guard,
  logging and `SendSystemChatMessage` delivery in the runtime.
  After re-reading, the agent must return exactly `NO_REPLY`; maintenance updates
  do not need a spoken acknowledgment. Add a thin `runtimes/<name>/skill_watcher.go` parallel to `runtimes/openclaw/skill_watcher.go`
  — only the **target dir** and the **notify path** differ. Gate with
  `skills.Supported(device.Capabilities(...))`. Advance a watched version only after
  download and extraction succeed, so a transient failure retries on the next poll.
  Notify the agent with `SendSystemChatMessage`.

---

### Jev skill preloading across runtimes

Skill preloading belongs to the runtime's hook or managed bridge, after OS
hardware-intent routing. It is separate from `jev_intent`: selecting a skill
never executes a tool or grants permission. Keep Hermes's existing native plugin
when adding another runtime; do not also classify the same turn in os-server.

- Hermes uses its native `pre_llm_call` hook and `skill_view` loader.
- PicoClaw uses a native `before_llm` process hook and the skill roster advertised
  in its system messages. It shares Hermes's Python decision implementation.
- Codex, Claude Code and OpenCode preload in their OS-managed bridges. They share
  `system/lib/jevskills` as a library, with separate runtime adapters and native policy
  checks. This covers ordinary chat sent through those bridges; standalone CLI
  sessions and Telegram `/coding` sessions retain native discovery.
- OpenClaw's native plugin and its opt-in/compatibility requirements are described
  in [OpenClaw Jev preloading](openclaw.md).

The Go adapters use the configured OS proxy at `llm_base_url + /jev/decisions`,
with `llm_api_key`; only current request text and skill names/descriptions leave
the device. For recognized voice envelopes, only the authoritative instruction
is classified; contradictory transcripts and appended Harness routing/result
metadata are excluded. Empty/malformed envelopes and context-dependent fragments
(such as “brighter” or “continue”) defer to the runtime. The original forwarded
request remains unchanged. Skill bodies are loaded locally. There are no retries, redirects,
direct-provider fallbacks or conversation-history uploads. The total selection
budget is at most 3 seconds, with a 30-second error cooldown and a non-blocking
busy gate. Selection requires probability >= 0.70, margin >= 0.20 and independent
fit >= 0.60. More than 32 eligible skills causes abstention, not roster truncation.
These are skill-discovery thresholds, not the stricter hardware-intent thresholds.

A preload includes the complete bounded SKILL.md and its absolute directory.
The Go adapters only preload simple static skills from the runtime's installed
root; dynamic templates, custom frontmatter controls, conflicting project skill
roots and unsupported native policy settings defer to native discovery. They
recheck eligibility and content after inference. System/slash/attachment turns
keep their existing route. All new non-Hermes integrations are **disabled by
default** pending native validation; Hermes is unchanged. Each new runtime owns a Go build switch `const jevEnabled = false`: bridges use `gatewayd/jev.go`, PicoClaw uses `jev_hook.go`, and OpenClaw uses `jev_plugin.go`. When disabled, bridges construct no selector; native onboarding installs no assets and creates no Jev registration. If an older Jev registration exists, Go only disables that registration and preserves unrelated settings. Disabled integrations do not read skills, call the provider, or add Jev content to requests. Enabling requires changing the switch, rebuilding and restarting through runtime management; env/config is not a replacement for the build switch. `JEV_CONFIG_PATH` selects the bridge OS config file.

Bind a result to the originating request, preserve source/session identity, and
reuse it only for a retry of that same request. Never persist a selected skill
as an AGENTS.md or persona update. Native history may retain supplied skill text
like an ordinary skill read; this does not make it selected for subsequent turns.
Test selected/abstain/error paths, native policy opt-outs, timeout, tool loops,
retry, session isolation and source/attachment routing with mocks before release.

## 6. Hooks — OS-side reimplementation for Hermes (a worked example)

OpenClaw hooks (`hooks/<name>/{HOOK.md, handler.ts}`) are TypeScript handlers that
fire on OpenClaw's `message:preprocessed` event — `emotion-acknowledge` (instant
"thinking" face on message arrival) and `turn-gate` (set busy for channel turns).
They are **runtime-specific** and not portable. A new backend does **not** inherit
them.

**Why OpenClaw needs hooks but Hermes does not.** In OpenClaw every turn runs
inside the daemon; os-server pushes the message over WebSocket and loses the
thread, so the only place to inject "thinking" at the right moment is the hook.
With Hermes the interception point is already on the Go side — **every turn sent
to Hermes flows through `runtimes/hermes/chat.go:sendChat`** (voice, sensing, web,
and Telegram, whose receive loop is Lumi-side — see `telegramRunOrigin`). So we
reimplement the hook natively in Go and fire from `sendChat`, instead of
materializing hook files into a workspace (Hermes has no Go onboarding to do that,
and no `~/.hermes/hooks/HOOK.yaml` loader to execute them — the `handler.ts`
copied in by `claw migrate` is dead weight under Hermes).

What was done:
- **`emotion-acknowledge` → native Go** in `runtimes/hermes/emotion_ack.go`
  (`fireAckEmotion`, called from `sendChat`). It mirrors `handler.ts` 1:1: same
  emotion (`thinking`, intensity `0.7`), same skip prefixes
  (`[sensing:`/`[activity]`/`[emotion]`/`[speech_emotion]` + empty), and the
  capability gate goes through the **shared registry** `skills.SupportedHooks`
  (→ `HookCapability["emotion-acknowledge"] = expression`), resolved once at
  construction (`Service.ackHookEnabled`). The TS hook's `[HANDLED]` text skip
  maps to the Go-native `IsSilentRun(runID)` check — os-server signals
  realtime-handled turns via `MarkSilentRun`, not a body marker.
- **`turn-gate` → not mirrored (redundant).** `sendChat` already marks the turn
  busy (`busySince`/`activeTurn`) before the network round-trip, so a separate
  gate would duplicate it.

> ⚠️ **Maintenance coupling — no compile-time link.** `runtimes/openclaw/hooks/emotion-acknowledge/
> handler.ts` (OpenClaw) and the `emotion_ack.go` files in
> `runtimes/{hermes,picoclaw,codex,claudecode,opencode}` are independent
> implementations of the same behavior. **When you change one, change them
> all** — skip rules, emotion name/intensity, and capability gate must stay
> identical, or the backends drift apart silently. Keep the cross-reference
> comments in every file.

**When a backend-native (Python) hook *would* be needed.** Only for turns that
originate **inside** the backend and never pass through `sendChat` — e.g. a
backend heartbeat or a channel the backend listens on directly (not proxied by
Lumi). The lamp has none of these today, so the Python-plugin path
(`pre_gateway_dispatch` in `~/.hermes/plugins/`, discovered by Hermes; there is
**no** `~/.hermes/hooks/HOOK.yaml` loader in the shipped build — verify the actual
loader before assuming) is **deferred as YAGNI**, not a parity gap. Build it only
when such a turn source appears.

---

## 7. Factory reset

- Implement the backend wipe **in `ResetAgent()`** (`runtimes/<name>/reset.go`).
  `server/system/factoryreset.go` resolves the **active gateway** and calls
  `gw.ResetAgent()` — there is **no per-backend `switch`** to keep in sync (adding
  a backend = implement `ResetAgent`, nothing in `server/system`). A backend whose
  state is owned externally (PicoClaw) ships a **no-op `ResetAgent`**, so it is
  correctly left untouched (the old `switch` defaulted PicoClaw to the OpenClaw
  wipe — a latent bug this removed). The one shared primitive, `osreset.WipePath`,
  lives in `lib/osreset` so a backend can use it without importing `server/system`.
- **Your persona is wiped for you — but only if `personaPaths` is right.**
  `ResetAgent()` reaches the *active* backend alone, yet persona files are
  **copies**: every runtime switch migrates SOUL/IDENTITY/MEMORY/USER/KNOWLEDGE
  into the destination and leaves the source's copy behind, so a device that has
  switched backends holds the same profile in several trees. Clearing only the
  active one lets the next switch migrate a stale copy straight back in (device
  observed 2026-09-03: a retired owner's name in four byte-identical `USER.md`
  files, oldest from 2026-07-08, still being spoken by the lamp).
  `factoryreset.go` therefore also calls `migratepersona.PersonaPaths`, which
  unions `personaPaths(opts)` across **all** registered adapters. That method is
  on the `runtimeAdapter` interface, so the compiler makes you write it — but it
  cannot check that you listed the right files. Two rules:
  - List **files**, plus the `memory/` dir. **Never the workspace or home root** —
    those also hold `skills/`, `configs/` and, for Hermes, the installation
    itself. `TestPersonaPathsNeverWipeARuntimeRoot` enforces this.
  - Returning `nil` compiles and silently leaks the profile through a reset;
    `TestEveryAdapterContributesPersonaPaths` catches it.
- **Wipe `/root/config/agent_state.json` in lockstep with `config.json`** — they
  are a pair (current runtime + switch history). Leaving `agent_state.json` while
  `config.json` resets makes a stale `prev` diverge from the reset `current` and
  triggers a **spurious persona migration** that propagates wiped/stub state.
- Keep what must survive (`bootstrap.json` = OTA state).
- A wipe path that removes migrated content (skills, config) must have a
  **restore path that runs after the reset** (presync, §3/§5) — or the content is
  gone for good once `install.sh` stops re-running.

---

## 8. Capability gating

Use the runtime-agnostic platform metadata in `system/skills`:
- `skills.Supported(deviceCaps)` for skills, `skills.SupportedHooks(deviceCaps)`
  for hooks, where `deviceCaps = device.Capabilities(config.DeviceTypeOrDefault())`.
- Never hardcode a skill/hook list per backend — gate the same way OpenClaw does.

---

## 9. Channels — capability model + switch re-apply

Channels are a **declared capability**, not a silent best-effort. Each backend
states which messaging channels it can run, and the OS uses that declaration both
to reject unsupported setups up front and to re-apply the surviving ones when the
runtime changes.

### The capability declaration

- **`SupportedChannels() []string`** (new `AgentGateway` method,
  `domain/agent.go`) — returns the channels the runtime can run. Per backend:
  - openclaw → `[telegram, slack, discord, whatsapp]` (`runtimes/openclaw/channels.go`)
  - hermes → `[telegram, slack, discord]` (`runtimes/hermes/channels.go`)
  - picoclaw → `[telegram]` (`runtimes/picoclaw/channels.go`)
  - claudecode → `[telegram, slack, discord]` (`runtimes/claudecode/channels.go`
    — all device-owned, mirroring codex: `telegram_poll.go` getUpdates loop,
    `discord.go` discordgo session, `slack.go` MQTT bridge; Claude Code's
    native channel plugins are deliberately not used)
  - opencode → `[telegram, slack, discord]` (`runtimes/opencode/channels.go`
    — all device-owned, mirroring codex/claudecode)
- Helper `domain.ChannelSupported(gw, channel) bool` (`domain/channel.go`) — the
  one place callers test membership.
- Shared sentinels in package `domain` (`domain/channel.go`):
  `ErrChannelNotSupported` (`"channel_not_supported"`) and
  `ErrChannelCredentialsMissing` (`"channel_credentials_missing"`). Compared by
  the runtimes, the device layer, and the MQTT handlers, so everyone tests one
  value.

> **No more silent no-op.** The old behavior was for a backend to accept any
> channel and quietly return `nil` for ones it couldn't run. That hid dead config.
> Now `AddChannel`/`RefreshChannelConfig` **MUST** return
> `domain.ErrChannelNotSupported` for a channel not in `SupportedChannels()`.

### Capability-aware `AddChannel` / `RefreshChannelConfig`

A **supported** channel is applied; an **unsupported** one returns
`domain.ErrChannelNotSupported` from the gateway. The device layer
(`system/device/channels.go` `AddChannel`) now gates on `SupportedChannels()`
**before** persisting credentials, so an unsupported channel never leaves a dead
token in `config.json`. The order is:

1. **Capability gate** — `domain.ChannelSupported(gw, channel)`; reject with
   `ErrChannelNotSupported` before touching `config.json`.
2. **Persist creds → `config.json`** (the channel's tokens).
3. **`gateway.AddChannel`** — apply in the active runtime.

Persist-**before**-apply matters because a config-reading apply (hermes' presync
re-reads `config.json` to rebuild `~/.hermes/.env`) must see the new tokens, and a
transient apply failure then leaves creds persisted — the recoverable direction
(boot presync / `ChannelReconcile` re-applies them).

### Switch self-heal: `ChannelReconcile`

`ChannelReconcile` (`system/agent/channel_reconcile.go`) is a sibling of
`PersonaMigration`. It runs in the startup sequence **right after**
`personaMigration.Reconcile()` (`server/config_watch.go`). It is **non-blocking**.

On a **runtime switch** — detected when `config.AgentRuntime` !=
`config.ChannelsAppliedRuntime` — it re-applies each channel configured in
`config.json` to the new runtime via the gateway's `AddChannel` (the **full
provision path**). The load-bearing case is switching **into openclaw**: its
slack/discord plugins are installed on demand by `AddChannel`, so a config-only
refresh would **not** install them (`install.sh` doesn't pre-bundle those
plugins). Switching into hermes is largely self-healed already — its presync
re-syncs `.env` before the gateway starts, so the re-apply is an idempotent no-op.

- Channels the new runtime **can't run** are collected into
  `config.ChannelsUnsupported` and surfaced on the MQTT info uplink
  (`unsupported_channels` field on `MQTTInfoResponse`, `domain/device.go`).
- Creds for unsupported channels are **left in `config.json`**, so switching back
  restores them.
- The marker `config.ChannelsAppliedRuntime` advances **once per switch on a clean
  pass**; a transient apply failure leaves it un-advanced so the next boot retries.

### New `config.json` fields

`channels_applied_runtime` and `channels_unsupported`
(`server/config/config.go`).

### Adding a new runtime

- Implement `SupportedChannels()` to declare **real** capability.
- Make `AddChannel`/`RefreshChannelConfig` return `domain.ErrChannelNotSupported`
  for anything not in that list — never a silent no-op.
- **Telegram is device-owned** on hermes/picoclaw: the receive loop is driven by
  `config.TelegramBotToken`, so the runtime needs no write — an honest success
  no-op inside `AddChannel`/`RefreshChannelConfig` (still gated on the list).
  On claudecode the loops are **runtime-owned** (Claude Code's channel
  plugins): `AddChannel` re-runs the embedded presync, which writes the token +
  allowlist into `~/.claude/channels/<ch>/` and restarts the bridge only on a
  hash-diff (see `docs/agentic/claudecode.md` §7).

---

## Checklist for a new backend

- [ ] `runtimes/<name>/` package; `*Service` implements **all** of `AgentGateway`.
- [ ] Every stub is `// no-op because …` or `// TODO(<name>-…)` — no bare bodies.
- [ ] N/A stubs that return an error return `domain.ErrNotSupportedByRuntime`,
      never `nil` (§4 "No fake success").
- [ ] `domain.AgentRuntime<Name>` + `AgentRuntimes` entry; `factory.go` case.
- [ ] `install.sh` + `install.go` (`//go:embed` + `runtimereg.Register`).
- [ ] **Stateful setup → `presync.sh`** (`//go:embed` + `runtimereg.RegisterPresync`),
      materialized by os-server every switch. Nothing reset-fragile lives only in
      `install.sh`.
- [ ] `verify` hook is cheap (CLI presence), not a structure check.
- [ ] `migrate_persona/runtime_<name>.go` (copy `runtime_example.go`): ONE read +
      ONE write adapter (bundle ↔ layout), registered in the `adapters` map. `read` surfaces SOUL, identity
      fields, MEMORY/USER, and any KNOWLEDGE/daily slots; `write` restores each to
      its native slot (identity → its own file, or inline if no slot) and folds
      slots the backend lacks. `Overwrite=true` for SOUL. No new `Direction` enum.
- [ ] Skills: copy-import + **restore-in-presync** (guarded) + `skill_watcher.go`
      (parallel to openclaw, shared `system/skills/skillzip.go`).
- [ ] Hooks: backend-native or OS-side — decided & documented (not silently absent).
      If reimplemented OS-side in Go (no compile-time link to the TS hook), add
      cross-reference comments in both files so a change to one flags the other.
- [ ] `ResetAgent()` in `runtimes/<name>/reset.go` (factory-reset calls
      `gw.ResetAgent()` on the active gateway — no `factoryreset.go` switch); **`agent_state.json` wiped with
      `config.json`**.
- [ ] `personaPaths(opts)` on the persona adapter lists this runtime's persona
      **files** + `memory/` dir — never the workspace/home root — so a factory
      reset clears the profile in *every* tree, not just the active one (§7).
- [ ] `userProfilePath(opts)` points at this runtime's real `USER.md` (Hermes
      keeps it under `memories/`) so the startup enrollment reconcile can retire
      a profile whose person no longer has a face/voice enrollment (§7).
- [ ] Adapter implements memoryFilePath + workspaceRoot; run
      `go test ./system/agent/migrate_persona/` (`TestMemoryFilePathsCoverEveryAdapter`).
- [ ] **People sync** in this runtime's own OS-managed instruction block: keep
      `USER.md`'s `## Users` section current, as `- **<label> (friend)**: …`
      keyed by the enrollment label, add/update only, never cross-attribute,
      never fill `**Name:**`. The slot differs per runtime — HEARTBEAT.md where
      there is a heartbeat loop, CLAUDE.md for claudecode (no loop), the SOUL.md
      block for hermes (no loop, no KNOWLEDGE.md).
      `TestEveryRuntimeTeachesThePeopleSync` fails if a runtime ships without it.
- [ ] **Device soul injected on every boot**, from `device.ResolveSoul(deviceType)`
      (`system/device/soul.go`) — the shared `soul_ref` resolver. Do this in
      onboarding, not only in factory reset, and do NOT rely on persona
      migration: migration copies from a PREVIOUS runtime, so a device that
      boots straight into yours has nothing to copy from and comes up with no
      persona at all. Hermes shipped that way and its lamps lost every
      skill-routing rule (`[sensing:*]` → `skills/sensing/SKILL.md`) until
      `ensureSoulMDBlock` was added.
- [ ] **Discard the backend's OWN default soul** before keeping what sits below
      your block. Most backends re-seed a default persona whenever their prompt
      file is missing, and presync runs before onboarding — so a freshly flashed
      device hands you that seed, not an empty file. Kept, it becomes a second
      persona contradicting the device one. Check the shape on a real device:
      Hermes' seed opens with prose, not a heading, which is why
      `managedDefaultSoulPrefixes` is a prefix list where openclaw only needed
      `isDefaultSoulHeading`.
- [ ] **One delimiter per OS-managed block.** Every runtime wraps its blocks in
      `<!-- OS DO NOT REMOVE -->`…`---`. If yours owns a SECOND block in the same
      file, give it its own marker — a shared one makes each updater strip the
      other's block. Hermes hit exactly this: its skill-priority block deleted
      the persona on the next boot.
- [ ] Capability gating via `skills.Supported` / `SupportedHooks`.
- [ ] **Channels (§9):** `SupportedChannels()` declares real capability;
      `AddChannel`/`RefreshChannelConfig` return `domain.ErrChannelNotSupported`
      for channels not on the list (no silent no-op). Telegram is device-owned on
      hermes/picoclaw (success no-op, no runtime write); runtime-owned on
      claudecode (presync re-sync + hash-diff restart).
- [ ] Notify the agent on skill change via `SendSystemChatMessage`.
- [ ] Docs: update `docs/agentic/hermes.md`-style backend doc + this checklist if the
      contract changed.

---

## Hermes parity status (honest ledger)

**Done:** switch wiring, embedded install + presync (config self-heal, skill
restore), persona/memory migration (SOUL + inline identity, MEMORY + daily +
KNOWLEDGE, USER), `UpdateIdentityName`, skill watcher, factory-reset
`agent_state.json` lockstep, hooks (`emotion-acknowledge` reimplemented OS-side in
Go; `turn-gate` redundant — §6), `WriteMCPEntry`/`RemoveMCPEntry` (`config.yaml`
`mcp_servers`) + MCP clone-on-switch (`MCPReconcile`).

**Still open / no-op:** backend-native (Python) hooks for backend-internal turn
sources (deferred YAGNI — §6), `CompactSession`,
`FetchChatHistory`, and the model-sync group (`StartModelSync`,
`UpdatePrimaryModel`, `StartPrimaryModelWatch`, `RefreshModelsConfig` — largely
N/A because os-server sends a fixed request model to the campaign-api custom
provider). The error-returning ones (`CompactSession`, `UpdatePrimaryModel`,
`RefreshModelsConfig`, `GetConfigJSON`) return
`domain.ErrNotSupportedByRuntime` rather than fake success (§4).
