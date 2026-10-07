# OpenClaw Jev skill preloading

When enabled, OS onboarding installs the native `autonomous-jev` plugin under
`<OpenclawConfigDir>/extensions/autonomous-jev`. It registers
`before_prompt_build`, covering both OS-dispatched requests and channels handled
directly by OpenClaw. **Disabled by default** by `const jevEnabled = false` in `runtimes/openclaw/jev_plugin.go`. When disabled, bridges construct no selector; native onboarding installs no assets and creates no Jev registration. If an older Jev registration exists, Go only disables that registration and preserves unrelated settings. Disabled integrations do not read skills, call the provider, or add Jev content to requests. Enabling after validation requires changing the Go switch, rebuilding and restarting through runtime management.

If `plugins.allow` is configured, include `autonomous-jev` in that list too.
`plugins.deny`, plugin disables and `plugins.enabled: false` remain authoritative.
Opt-in authorizes sending the **current request and eligible skill names and
descriptions** to the existing OS `llm_base_url` plus `/jev/decisions`, using the
configured OS API key. There is no alternate provider or endpoint fallback.
The on-disk plugin sidecar contains only the OS config file path, not credentials.
Full skill contents stay local and are added to the current native request.

The plugin reads the host-produced session skill snapshot, checks the active
session ID, and intersects resolved skills with the native advertised roster.
It rechecks config and files after selection. Simple workspace, bundled, managed
and plugin skills can participate, using the exact canonical `SKILL.md` paths
resolved by the native snapshot; no workspace-directory restriction applies.
Native snapshot ordering resolves duplicate names. Skills with
dependency/platform metadata, custom invocation policy, unknown tool policies,
active sandboxing, symlinks or oversized files defer to normal native loading.
Missing/incompatible snapshots also defer, including runtimes that do not expose
the required native session accessors. There is no filesystem-wide skill scan.

There is no 32-candidate cutoff. The full serialized UTF-8 request must fit
256 KiB; an oversized request defers before credential access or network I/O,
without truncating the roster. Skill files remain bounded to 32 KiB; responses
and preload envelopes remain bounded to 64 KiB. Choice probability must be at least 0.70, margin at least
0.20 and independent fit at least 0.60, matching Hermes skill selection. The
complete operation has a 3-second deadline, no retry, one in-flight operation,
and a 30-second error cooldown. Timeout, malformed responses, abstention or
changed eligibility leave the original request unchanged.

Only the current hook request is classified; history is never inspected to find
a replacement request. Explicit skill/slash commands, system/sensing markers,
known image/attachment markers and context-only follow-ups such as `brighter`,
`continue`, or `make it brighter` bypass preloading. The main agent retains the
original conversation context. The selected full skill, source path and reference
directory are added through native `prependContext`, without replacing the system
prompt or granting tool permission. Native history may retain that skill read;
the selector never reuses a previous turn's decision. OS run correlation removes
only the validated preload envelope before matching its original pending request.

Logs use `[openclaw-jev] outcome=preloaded reason=accepted skill=...`, or
`abstained`, `skipped`, and `error`, without logging prompts or credentials.

Verified locally with mocked provider responses and native snapshot fixtures:

```sh
node --test runtimes/openclaw/plugins/jev/index.test.mjs
go test ./runtimes/openclaw -run 'TestJev|TestPending' -count=1
```

The native contract was inspected in installed OpenClaw 2026.2.23 and the
[upstream hook types](https://github.com/openclaw/openclaw/blob/main/src/plugins/hook-before-agent-start.types.ts).
No live Jev, device deployment or full gateway/channel integration test is implied
by these local checks. Older hook versions do not expose structured attachment
metadata; only supplied attachment fields and textual markers can be recognized.

## Workspace attestation (OpenClaw >= 2026.9)

OpenClaw records an attestation in `/root/.openclaw/state/openclaw.sqlite`
(`workspace_setup_state` and related tables) when it seeds the workspace. For
24 hours afterwards, `openclaw onboard` refuses to reseed a workspace that looks
wiped (`WorkspaceVanishedError: … Refusing to reseed BOOTSTRAP.md over a recently
attested workspace`), which fails setup with `agent_setup_failed`.

- **Factory reset** (`runtimes/openclaw/reset.go`) wipes `/root/.openclaw/state`
  together with the workspace, so setup right after a reset can onboard again.
- **Image builds** (`scripts/imager/build*.sh`) run `openclaw onboard` in the
  chroot (180 s cap), then delete the workspace rows from the state database and
  the legacy attestation files. An image built hours before first setup therefore
  never carries a recent attestation, even when the chroot onboard timed out
  and left the workspace empty.

Device already stuck on this error: `sudo rm -rf /root/.openclaw/state
/root/.openclaw/workspace-attestations`, then run setup again.

## Reply prefix and heartbeat replies

- **No reply prefix.** Setup used to write `messages.responsePrefix: "auto"`,
  which OpenClaw resolves to the agent identity name; with no named agent that
  is the id, so every chat reply started with `[main]`. OpenClaw `doctor` also
  copies the global value into `channels.<channel>.responsePrefix`. Setup no
  longer writes it, and onboarding (`ensureMessagesQueueConfig`) removes `"auto"`
  from `messages`, every channel and every channel account on existing devices,
  then restarts the gateway. Custom prefixes are kept.
- **Heartbeat ends with `NO_REPLY`.** The OS block in `workspace/HEARTBEAT.md`
  used to say "skip silently", and the model sometimes answered with an empty
  message. OpenClaw treats an empty heartbeat as `agent-runner-failure` and posts
  `[main] ⚠️ Agent couldn't generate a response. Please try again.` to the last
  chat. The block now tells the agent to reply exactly `NO_REPLY` when the pass
  is done, OpenClaw's silent-heartbeat token. Onboarding rewrites the block on
  existing devices because its text changed.
- **Device SOUL must fit the bootstrap cap.** Setup and onboarding both use
  `agents.defaults.bootstrapMaxChars = 24000` and
  `agents.defaults.bootstrapTotalMaxChars = 48000`, from shared constants in
  `runtimes/openclaw/onboarding.go`. These are character budgets, not token limits.
  The 24k per-file cap fits the lamp SOUL (18,615 characters) with room for OS
  markers and owner edits. The lamp SOUL plus the managed AGENTS and HEARTBEAT
  blocks total about 27.9k characters; 48k leaves about 20k for other bootstrap
  files and additional content. This is a sizing allowance, not a measured
  latency optimum or a guarantee that an arbitrarily large workspace fits.
  Both the per-file and total caps still apply; longer files can lose their
  middle (OpenClaw keeps head and tail). Existing devices receive the updated
  limits on onboarding after deployment, which requests a gateway restart when
  defaults change. `TestDeviceSoulsFitTheBootstrapCap` checks every device SOUL,
  including lamp, against 23,000 characters, reserving 1,000 for OS markers and
  the owner's `## Personal` section. The intern-v2 SOUL remains about 10.5k;
  audio tags are limited to spoken replies, not channel or web chat replies.
